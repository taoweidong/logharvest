import socket
import zipfile
from pathlib import Path
from threading import Event

import pytest

import src.worker.main as worker_main
from src.application.worker.ports import ClaimedTask
from src.application.worker.processor import LeaseLost
from src.domain.rules.evaluator import compile_ruleset
from src.domain.rules.models import RuleSetConfig
from src.worker.main import WorkerRuntime


class FakeRepo:
    def __init__(self, tasks, draining=False, stop=None):
        self.tasks = list(tasks)
        self.draining = draining
        self.stop = stop
        self.claims = []
        self.completed = []
        self.worker_heartbeats = []
        self.registered = []
        self.recovered = []

    def is_draining(self, worker_id):
        return self.draining

    def claim_next(self, worker_id, worker_version):
        self.claims.append((worker_id, worker_version))
        if self.stop is not None:
            self.stop.set()
        if self.tasks:
            return self.tasks.pop(0)
        return None

    def heartbeat_worker(self, worker_id, status, current_task_id=None):
        self.worker_heartbeats.append((status, current_task_id))

    def heartbeat(self, *args, **kwargs):
        return True

    def complete(self, task_id, attempts, status, processed_lines, matched_rows, error=None):
        self.completed.append((task_id, attempts, status, processed_lines, matched_rows, error))

    def register_worker(self, worker_id, hostname, version):
        self.registered.append((worker_id, hostname, version))

    def recover_stale(self, stale_seconds=120):
        self.recovered.append(stale_seconds)
        return 0


class FakeWriter:
    def __init__(self):
        self.deleted = []
        self.written = []

    def delete_for_task(self, task_id):
        self.deleted.append(task_id)

    def write_batch(self, matches):
        self.written.extend(matches)


def make_task(tmp_path: Path) -> ClaimedTask:
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("app.log", "ERROR one\n")
    return ClaimedTask(
        id=1,
        job_id=2,
        source_path=archive,
        file_name="a.zip",
        attempts=1,
        rule_set_id=3,
        rule_version=1,
        rule_config={
            "name": "t",
            "file_glob": "**/*.log",
            "rules": [{"id": "e", "match": {"type": "keyword", "any": ["ERROR"]}}],
        },
    )


def make_runtime(repo, writer, tmp_path: Path, **overrides) -> WorkerRuntime:
    options = {
        "repo": repo,
        "writer": writer,
        "worker_id": "w1",
        "worker_version": "0.1.0",
        "work_dir": tmp_path / "work",
        "rules_factory": lambda task: compile_ruleset(
            RuleSetConfig.model_validate(task.rule_config)
        ),
    }
    options.update(overrides)
    return WorkerRuntime(**options)


def test_run_once_processes_claimed_task(tmp_path):
    repo = FakeRepo([make_task(tmp_path)])
    writer = FakeWriter()
    runtime = make_runtime(repo, writer, tmp_path)

    assert runtime.run_once() is True

    assert len(writer.written) == 1
    assert repo.completed == [(1, 1, "succeeded", 1, 1, None)]
    assert repo.worker_heartbeats == [("busy", 1), ("online", None)]
    assert repo.claims == [("w1", "0.1.0")]


def test_run_once_does_not_claim_while_draining(tmp_path):
    repo = FakeRepo([make_task(tmp_path)], draining=True)
    writer = FakeWriter()
    runtime = make_runtime(repo, writer, tmp_path)

    assert runtime.run_once() is False

    assert repo.claims == []
    assert repo.completed == []
    assert repo.worker_heartbeats == [("draining", None)]
    assert writer.written == []


def test_run_once_returns_false_when_queue_is_empty(tmp_path):
    repo = FakeRepo([])
    runtime = make_runtime(repo, FakeWriter(), tmp_path)

    assert runtime.run_once() is False

    assert repo.worker_heartbeats == [("online", None)]


def test_run_once_records_failed_task_when_rules_cannot_compile(tmp_path):
    def broken_rules(task):
        raise ValueError("invalid rule config")

    repo = FakeRepo([make_task(tmp_path)])
    runtime = make_runtime(repo, FakeWriter(), tmp_path, rules_factory=broken_rules)

    assert runtime.run_once() is True

    assert repo.completed == [(1, 1, "failed", 0, 0, "invalid rule config")]
    assert repo.worker_heartbeats == [("busy", 1), ("online", None)]


def test_run_once_continues_after_lease_is_lost(tmp_path, monkeypatch):
    class LeaseLostProcessor:
        def __init__(self, **kwargs):
            pass

        def process(self, task):
            raise LeaseLost("lease lost")

    monkeypatch.setattr(worker_main, "TaskProcessor", LeaseLostProcessor)
    repo = FakeRepo([make_task(tmp_path)])
    runtime = make_runtime(repo, FakeWriter(), tmp_path)

    assert runtime.run_once() is True

    assert repo.completed == []
    assert repo.worker_heartbeats == [("busy", 1), ("online", None)]


def test_run_forever_registers_recovers_and_stops(tmp_path):
    stop = Event()
    repo = FakeRepo([], stop=stop)
    runtime = make_runtime(repo, FakeWriter(), tmp_path, poll_seconds=0.01)

    runtime.run_forever(stop)

    assert repo.registered == [("w1", socket.gethostname(), "0.1.0")]
    assert repo.recovered == [120]
    assert repo.worker_heartbeats == [("online", None), ("offline", None)]


def test_run_forever_marks_worker_offline_when_loop_raises(tmp_path):
    repo = FakeRepo([])
    runtime = make_runtime(repo, FakeWriter(), tmp_path)

    def boom():
        raise RuntimeError("claim failed")

    runtime.run_once = boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="claim failed"):
        runtime.run_forever(Event())

    assert repo.worker_heartbeats[-1] == ("offline", None)
