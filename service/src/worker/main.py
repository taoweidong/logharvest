import os
import shutil
import signal
import socket
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Event
from time import monotonic, time

from src.application.worker.ports import ClaimedTask, WorkerStats
from src.application.worker.processor import (
    DEFAULT_MAX_MATCHES_PER_TASK,
    LeaseLost,
    TaskProcessor,
)
from src.config.settings import settings
from src.domain.archive.reader import ArchiveBudget
from src.domain.rules.evaluator import CompiledRuleSet, compile_ruleset
from src.domain.rules.models import RuleSetConfig
from src.domain.source_guard import POLICY_ENFORCE
from src.infrastructure.database import engine
from src.infrastructure.match_writer import PostgresMatchWriter
from src.infrastructure.task_queue import PostgresTaskRepository
from src.worker.logging_setup import configure_worker_logging
from src.worker.metrics import collect_worker_stats

# Copying refreshes the lease at least every processor copy-heartbeat interval
# (5s by default), so a live worker is never 120s stale. The periodic sweep
# below then reclaims work owned by processes that actually died or wedged.
DEFAULT_STALE_SECONDS = 120
DEFAULT_RECOVER_INTERVAL_SECONDS = 300.0
# Local copies left by a previous run of this worker id are removed at startup
# once they are older than this, so a killed process cannot fill the temp disk.
ORPHAN_MAX_AGE_SECONDS = 3600.0


def _compile_task_rules(task: ClaimedTask) -> CompiledRuleSet:
    return compile_ruleset(RuleSetConfig.model_validate(task.rule_config))


class WorkerRuntime:
    """Claim-and-process loop for a single worker process."""

    def __init__(
        self,
        repo,
        writer,
        worker_id: str,
        worker_version: str,
        work_dir: Path,
        rules_factory: Callable[[ClaimedTask], CompiledRuleSet] = _compile_task_rules,
        poll_seconds: float = 1.0,
        stale_seconds: int = DEFAULT_STALE_SECONDS,
        recover_interval_seconds: float = DEFAULT_RECOVER_INTERVAL_SECONDS,
        batch_size: int = 1000,
        nas_read_only_policy: str = POLICY_ENFORCE,
        collect_stats: Callable[[Path], WorkerStats] | None = None,
        logger=None,
        archive_budget: ArchiveBudget | None = None,
        max_matches_per_task: int = DEFAULT_MAX_MATCHES_PER_TASK,
    ) -> None:
        self._repo = repo
        self._writer = writer
        self._worker_id = worker_id
        self._worker_version = worker_version
        self._work_dir = work_dir
        self._rules_factory = rules_factory
        self._poll_seconds = poll_seconds
        self._stale_seconds = stale_seconds
        self._recover_interval_seconds = recover_interval_seconds
        self._batch_size = batch_size
        self._nas_read_only_policy = nas_read_only_policy
        self._collect_stats = collect_stats
        self._logger = logger
        self._archive_budget = archive_budget
        self._max_matches_per_task = max_matches_per_task
        # 编译缓存：规则集版本不可变，(rule_set_id, version) 命中即免重复编译。
        # 5k 规则一次编译约 300ms+，2000 任务的同版本作业不缓存就是 10 分钟纯编译。
        self._rules_cache: dict[tuple[int, int], CompiledRuleSet] = {}

    def _stats(self) -> WorkerStats | None:
        """None 表示未注入采集器：轻量 fake 与测试保持零依赖。"""
        if self._collect_stats is None:
            return None
        return self._collect_stats(self._work_dir)

    def _log(self, level: str, event: str, **kwargs) -> None:
        if self._logger is None:
            return
        getattr(self._logger, level)(event, **kwargs)

    def _report_status(self, status: str, task_id: int | None = None) -> None:
        """向 workers 表上报状态；上报失败只记录，绝不允许反噬主循环。

        ``workers.last_heartbeat`` 只服务于管理视图与对端死 Worker 清扫，租约的权威
        判定是 ``file_tasks`` 上的 ``touch``（见 :mod:`processor`）。因此一次心跳
        抖动必须能被容忍：把异常抛回处理器，只会导致本可以跑完的长任务被记成失败。
        """
        try:
            self._repo.heartbeat_worker(self._worker_id, status, task_id, stats=self._stats())
        except Exception as exc:  # noqa: BLE001
            self._log("error", "worker_loop_error", stage="heartbeat", error=str(exc))

    def _get_rules(self, task: ClaimedTask) -> CompiledRuleSet:
        """按 (rule_set_id, version) 缓存编译结果；上限 64 个版本，FIFO 淘汰。"""
        key = (task.rule_set_id, task.rule_version)
        cached = self._rules_cache.get(key)
        if cached is None:
            cached = self._rules_factory(task)
            if len(self._rules_cache) >= 64:
                self._rules_cache.pop(next(iter(self._rules_cache)))
            self._rules_cache[key] = cached
        return cached

    def run_once(self) -> bool:
        """Claim and process at most one task; return whether work was attempted.

        ``False`` 同时也是"本轮没有进展"的信号，:meth:`run_forever` 借此退避一个轮询
        周期。仓库交互（drain 判定、认领、心跳）一律放进护栏：数据库抖动是常态，
        让它穿透到 :func:`main` 会让进程静默消失——已认领的任务就只能在 120 秒后
        等僵死回收（规格 §7.3 要求 Worker 长期驻留、故障自愈）。
        """
        try:
            if self._repo.is_draining(self._worker_id):
                # Keep the management view in sync without claiming new work.
                self._report_status("draining")
                return False

            task = self._repo.claim_next(self._worker_id, self._worker_version)
            if task is None:
                self._report_status("online")
                return False
            self._report_status("busy", task.id)
        except Exception as exc:  # noqa: BLE001
            self._log("error", "worker_loop_error", stage="claim", error=str(exc))
            return False

        self._log(
            "info",
            "task_claimed",
            task_id=task.id,
            job_id=task.job_id,
            file=task.file_name,
            attempt=task.attempts,
        )
        # 轻量测试替身可能没有 is_job_cancelled；缺失时不传取消检查。
        cancel_check = (
            (lambda: self._repo.is_job_cancelled(task.job_id))
            if hasattr(self._repo, "is_job_cancelled")
            else None
        )
        try:
            try:
                processor = TaskProcessor(
                    repo=self._repo,
                    writer=self._writer,
                    rules=self._get_rules(task),
                    work_dir=self._work_dir,
                    batch_size=self._batch_size,
                    nas_read_only_policy=self._nas_read_only_policy,
                    budget=self._archive_budget,
                    max_matches_per_task=self._max_matches_per_task,
                    cancel_check=cancel_check,
                    # A task can run for minutes; keep workers.last_heartbeat
                    # fresh so the management view and the peer dead-worker
                    # sweep do not see a live process as stalled.
                    on_lease_refresh=lambda: self._report_status("busy", task.id),
                )
            except Exception as exc:  # noqa: BLE001
                # A broken rule config or work-dir setup must not leave the
                # claimed task running until stale recovery notices it.
                self._repo.complete(task.id, task.attempts, "failed", 0, 0, str(exc))
                self._log(
                    "error",
                    "task_failed",
                    task_id=task.id,
                    job_id=task.job_id,
                    status="failed",
                    error=str(exc),
                )
                self._finalize_job(task.job_id)
            else:
                try:
                    result = processor.process(task)
                except LeaseLost:
                    # Another worker owns (or recovery returned) the task now.
                    self._log("warning", "task_lease_lost", task_id=task.id, job_id=task.job_id)
                else:
                    self._log(
                        "info",
                        "task_finished",
                        task_id=task.id,
                        job_id=task.job_id,
                        status=result.status,
                        processed_lines=result.processed_lines,
                        matched_rows=result.matched_rows,
                    )
                    if result.status == "failed":
                        # 永久失败不打断循环，但必须能从日志直接看到原因。
                        self._log(
                            "error",
                            "task_failed",
                            task_id=task.id,
                            job_id=task.job_id,
                            status="failed",
                            error=result.error,
                        )
                    self._finalize_job(task.job_id)
        finally:
            self._report_status("online")
        return True

    def _ensure_work_dir(self) -> None:
        """启动即建工作目录，空闲 Worker 也能报出临时盘余量。

        不建目录时 `shutil.disk_usage(work_dir)` 抛 FileNotFoundError，采集按
        "失败降级为 None"的约定把 `temp_disk_free_bytes` 报成 NULL，而 workers 表
        上报用的是 `COALESCE(:new, old)` —— 只要没有任务落过盘，这一列就一直是空，
        管理端 `/api/workers` 与 `/api/system/status` 看不到磁盘水位，偏偏派活之前
        最需要它。创建失败（临时盘没挂、属主不对）只记一条日志不阻断启动：真正写
        盘时任务路径依旧会报错，行为不比现在更差。
        """
        try:
            self._work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log(
                "warning",
                "work_dir_create_failed",
                work_dir=str(self._work_dir),
                error=str(exc),
            )

    def run_forever(self, stop_event: Event | None = None) -> None:
        stop = stop_event or Event()
        self._ensure_work_dir()
        self._cleanup_orphan_work_dirs()
        self._repo.register_worker(self._worker_id, socket.gethostname(), self._worker_version)
        self._recover_stale()
        self._sweep_finished_jobs()
        next_recover = monotonic() + self._recover_interval_seconds
        try:
            while not stop.is_set():
                # 兜底护栏：除了 KeyboardInterrupt / SystemExit（BaseException，
                # 必须放行才能让 SIGINT 与 SIGTERM 真正停下进程），其余任何异常都只
                # 记一条结构化日志后退避重试。漏网异常一旦穿透到这里，Worker 就会
                # 静默消失，而它手上的任务要等到僵死回收才有人接手。
                try:
                    now = monotonic()
                    if now >= next_recover:
                        # Periodic sweep so abandoned tasks are retried even when no
                        # worker process restarts, and jobs closed by recovery or
                        # cancellation are finalized without a completion callback.
                        self._run_periodic_maintenance()
                        next_recover = now + self._recover_interval_seconds
                    worked = self.run_once()
                except Exception as exc:  # noqa: BLE001
                    self._log("error", "worker_loop_error", stage="loop", error=str(exc))
                    worked = False
                # 出错和空闲都退避同一个轮询周期，避免对着一个坏掉的数据库刷日志。
                if not worked:
                    stop.wait(self._poll_seconds)
        finally:
            self._report_status("offline")

    def _run_periodic_maintenance(self) -> None:
        """周期性回收僵死任务与收尾作业；失败只记录，不能打断取任务的循环。"""
        try:
            self._recover_stale()
            self._sweep_finished_jobs()
        except Exception as exc:  # noqa: BLE001
            self._log("error", "worker_loop_error", stage="maintenance", error=str(exc))

    def _recover_stale(self) -> None:
        """Return tasks abandoned by dead workers before claiming new work.

        These methods are optional so lightweight test doubles remain valid; the
        PostgreSQL repository always exposes them.
        """
        recover = getattr(self._repo, "recover_stale", None)
        demote = getattr(self._repo, "mark_dead_workers", None)
        if not callable(recover):
            return
        recovered = recover(self._stale_seconds)
        # A worker that died cannot report itself offline.
        if callable(demote):
            demote(self._stale_seconds)
        if recovered:
            # Recovered tasks can be the last open rows of their jobs; close
            # those jobs now instead of waiting for the next sweep interval.
            self._sweep_finished_jobs()

    def _finalize_job(self, job_id: int) -> None:
        """Close the job if this was its last open file task."""
        finalize = getattr(self._repo, "finalize_job", None)
        if callable(finalize):
            finalize(job_id)

    def _sweep_finished_jobs(self) -> None:
        """Compensation for jobs whose last task ended through recovery or cancel."""
        sweep = getattr(self._repo, "finalize_finished_jobs", None)
        if callable(sweep):
            sweep()

    def _cleanup_orphan_work_dirs(self) -> int:
        """Delete local copies left behind by an earlier run of this worker id.

        A process killed mid-copy would otherwise keep multi-gigabyte archives
        on the temp disk until the disk fills up. Only directories older than the
        orphan limit are touched, and only under this worker's own work dir.
        """
        if not self._work_dir.is_dir():
            return 0
        cutoff = time() - ORPHAN_MAX_AGE_SECONDS
        removed = 0
        for entry in self._work_dir.iterdir():
            try:
                stale = entry.is_dir() and entry.stat().st_mtime <= cutoff
            except OSError:
                continue
            if stale:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed


def main() -> None:
    worker_id = os.getenv("LOGHARVEST_WORKER_ID") or socket.gethostname()
    base_dir = Path(os.getenv("LOGHARVEST_WORK_DIR", tempfile.gettempdir()))
    work_dir = base_dir / "logharvest" / worker_id
    runtime = WorkerRuntime(
        repo=PostgresTaskRepository(engine, max_concurrent_copies=settings.max_concurrent_copies),
        writer=PostgresMatchWriter(engine),
        worker_id=worker_id,
        worker_version=settings.worker_version,
        work_dir=work_dir,
        poll_seconds=settings.worker_poll_seconds,
        batch_size=settings.matches_batch_size,
        nas_read_only_policy=settings.nas_read_only_policy,
        collect_stats=collect_worker_stats,
        logger=configure_worker_logging(worker_id=worker_id),
        archive_budget=ArchiveBudget(
            max_member_bytes=settings.archive_max_member_bytes,
            max_total_bytes=settings.archive_max_total_bytes,
            max_members=settings.archive_max_members,
            max_ratio=settings.archive_max_ratio,
        ),
        max_matches_per_task=settings.max_matches_per_task,
    )

    stop = Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    runtime.run_forever(stop)


if __name__ == "__main__":
    main()
