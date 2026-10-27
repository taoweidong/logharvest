"""Worker 运行时的防御分支：配置编译、失败结果日志、维护清扫吞错、孤儿目录 stat 失败。

WorkerRuntime 的日志走注入的 ``logger``（见 ``_log``），所以这里沿用
test_runtime.py 的 RecordingLogger 惯例而不是 structlog capture。
"""

from types import SimpleNamespace

from test_runtime import FakeRepo, FakeWriter, make_runtime, make_task

import src.worker.main as worker_main
from src.application.worker.ports import TaskResult


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def _record(self, level: str, event: str, **kwargs) -> None:
        self.events.append((level, event, kwargs))

    def info(self, event: str, **kwargs) -> None:
        self._record("info", event, **kwargs)

    def warning(self, event: str, **kwargs) -> None:
        self._record("warning", event, **kwargs)

    def error(self, event: str, **kwargs) -> None:
        self._record("error", event, **kwargs)


def test_compile_task_rules_builds_compiled_ruleset(tmp_path) -> None:
    task = make_task(tmp_path)
    compiled = worker_main._compile_task_rules(task)
    assert [item.rule.id for item in compiled.rules] == ["e"]


def test_run_once_logs_failed_task_result_as_error(tmp_path, monkeypatch) -> None:
    class FailedProcessor:
        def __init__(self, **kwargs) -> None: ...

        def process(self, task) -> TaskResult:
            return TaskResult(
                status="failed", processed_lines=3, matched_rows=0, error="member blow-up"
            )

    monkeypatch.setattr(worker_main, "TaskProcessor", FailedProcessor)
    repo = FakeRepo([make_task(tmp_path)])
    logger = RecordingLogger()
    runtime = make_runtime(repo, FakeWriter(), tmp_path, logger=logger)

    assert runtime.run_once() is True

    errors = [(level, event, kw) for level, event, kw in logger.events if event == "task_failed"]
    assert errors and errors[0][0] == "error"
    assert errors[0][2]["error"] == "member blow-up"
    # 终态回写是真实 processor 的职责（complete 由其内部调用），
    # 运行时自身只负责失败日志与作业收尾。
    assert repo.finalized == [2]


def test_periodic_maintenance_swallows_sweep_errors(tmp_path) -> None:
    repo = FakeRepo([])
    logger = RecordingLogger()
    runtime = make_runtime(repo, FakeWriter(), tmp_path, logger=logger)

    def broken_recover() -> None:
        raise RuntimeError("queue connection lost")

    runtime._recover_stale = broken_recover  # type: ignore[method-assign]
    runtime._run_periodic_maintenance()  # 不抛

    errors = [
        (level, event, kw) for level, event, kw in logger.events if event == "worker_loop_error"
    ]
    assert errors and errors[0][2]["stage"] == "maintenance"
    assert "queue connection lost" in errors[0][2]["error"]


def test_recover_stale_skips_repos_without_the_capability(tmp_path) -> None:
    runtime = make_runtime(SimpleNamespace(), FakeWriter(), tmp_path)
    # 仓库没有 recover_stale/mark_dead_workers 能力时直接返回，不抛 AttributeError。
    runtime._recover_stale()


def test_cleanup_orphan_dirs_tolerates_stat_failures(tmp_path, monkeypatch) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    ghost = work_dir / "42"
    ghost.mkdir()

    real_stat = type(ghost).stat

    def broken_stat(self, *args, **kwargs):
        if self.parent == work_dir:
            raise OSError("vanished while scanning")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(ghost), "stat", broken_stat)
    runtime = make_runtime(FakeRepo([]), FakeWriter(), tmp_path)
    removed = runtime._cleanup_orphan_work_dirs()
    assert removed == 0  # stat 失败的目录按"不清理"处理，绝不抛

    monkeypatch.setattr(type(ghost), "stat", real_stat)
    assert ghost.exists()
    assert runtime._cleanup_orphan_work_dirs() == 0  # 新目录未过期，同样不清理
