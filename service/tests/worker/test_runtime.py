import os
import signal
import socket
import time
import zipfile
from pathlib import Path
from threading import Event

import pytest

import src.worker.main as worker_main
from src.application.worker.ports import ClaimedTask, TaskResult
from src.application.worker.processor import LeaseLost
from src.domain.rules.evaluator import compile_ruleset
from src.domain.rules.models import RuleSetConfig
from src.worker.main import WorkerRuntime


class FakeRepo:
    def __init__(self, tasks, draining=False, stop=None, recovered_count=0):
        self.tasks = list(tasks)
        self.draining = draining
        self.stop = stop
        self.recovered_count = recovered_count
        self.claims = []
        self.completed = []
        self.worker_heartbeats = []
        self.registered = []
        self.recovered = []
        self.demoted = []
        self.finalized = []
        self.swept = 0

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

    def touch(self, task_id, attempts):
        return True

    def heartbeat(self, *args, **kwargs):
        return True

    def complete(self, task_id, attempts, status, processed_lines, matched_rows, error=None):
        self.completed.append((task_id, attempts, status, processed_lines, matched_rows, error))

    def register_worker(self, worker_id, hostname, version):
        self.registered.append((worker_id, hostname, version))

    def recover_stale(self, stale_seconds=120):
        self.recovered.append(stale_seconds)
        return self.recovered_count

    def mark_dead_workers(self, stale_seconds=120):
        self.demoted.append(stale_seconds)
        return 0

    def finalize_job(self, job_id):
        self.finalized.append(job_id)
        return "succeeded"

    def finalize_finished_jobs(self):
        self.swept += 1
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
    assert repo.worker_heartbeats[0] == ("busy", 1)
    assert repo.worker_heartbeats[-1] == ("online", None)
    # A task that runs for minutes must keep refreshing workers.last_heartbeat,
    # so the peer dead-worker sweep never sees a live process as stalled.
    assert set(repo.worker_heartbeats[1:-1]) == {("busy", 1)}
    assert len(repo.worker_heartbeats) > 2
    assert repo.claims == [("w1", "0.1.0")]
    # The completing worker closes the job when it held the last open task.
    assert repo.finalized == [2]


def test_run_once_forwards_configured_batch_size(tmp_path, monkeypatch):
    captured = {}

    class RecordingProcessor:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def process(self, task):
            return TaskResult("succeeded", 0, 0)

    monkeypatch.setattr(worker_main, "TaskProcessor", RecordingProcessor)
    runtime = make_runtime(FakeRepo([make_task(tmp_path)]), FakeWriter(), tmp_path, batch_size=7)

    assert runtime.run_once() is True
    assert captured["batch_size"] == 7


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
    assert repo.finalized == [2]


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
    # A worker that lost the lease no longer owns the task, so it must not
    # decide the job's outcome.
    assert repo.finalized == []


def test_run_forever_registers_recovers_and_stops(tmp_path):
    stop = Event()
    repo = FakeRepo([], stop=stop)
    runtime = make_runtime(repo, FakeWriter(), tmp_path, poll_seconds=0.01)

    runtime.run_forever(stop)

    assert repo.registered == [("w1", socket.gethostname(), "0.1.0")]
    assert repo.recovered == [120]
    # A process that was SIGKILLed cannot report its own exit.
    assert repo.demoted == [120]
    assert repo.swept == 1
    assert repo.worker_heartbeats == [("online", None), ("offline", None)]


def test_run_forever_sweeps_jobs_right_after_recovering_tasks(tmp_path):
    stop = Event()
    repo = FakeRepo([], stop=stop, recovered_count=2)
    runtime = make_runtime(repo, FakeWriter(), tmp_path, poll_seconds=0.01)

    runtime.run_forever(stop)

    # Recovered tasks can be the last open rows of their jobs; closing them must
    # not wait for the next sweep interval.
    assert repo.recovered == [120]
    assert repo.swept == 2


def test_run_forever_recovers_stale_periodically(tmp_path):
    stop = Event()
    repo = FakeRepo([], stop=stop)
    runtime = make_runtime(
        repo,
        FakeWriter(),
        tmp_path,
        poll_seconds=0.01,
        recover_interval_seconds=0,
    )

    runtime.run_forever(stop)

    # One recovery on startup, then another sweep before the first poll because
    # the interval is zero.
    assert repo.recovered == [120, 120]
    assert repo.swept == 2


def test_run_forever_marks_worker_offline_when_loop_raises(tmp_path):
    repo = FakeRepo([])
    runtime = make_runtime(repo, FakeWriter(), tmp_path)

    def boom():
        raise RuntimeError("claim failed")

    runtime.run_once = boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="claim failed"):
        runtime.run_forever(Event())

    assert repo.worker_heartbeats[-1] == ("offline", None)


def test_main_wires_stop_event_and_signals(monkeypatch):
    captured = {}
    handlers = []

    class FakeRuntime:
        def __init__(self, **kwargs):
            captured["options"] = kwargs

        def run_forever(self, stop):
            captured["stop"] = stop

    monkeypatch.delenv("LOGHARVEST_WORKER_ID", raising=False)
    monkeypatch.setattr(worker_main, "WorkerRuntime", FakeRuntime)

    def fake_repo(engine, **kwargs):
        captured["repo_options"] = kwargs
        return object()

    monkeypatch.setattr(worker_main, "PostgresTaskRepository", fake_repo)
    monkeypatch.setattr(worker_main, "PostgresMatchWriter", lambda engine: object())
    monkeypatch.setattr(worker_main.socket, "gethostname", lambda: "test-host")
    monkeypatch.setattr(
        worker_main.signal,
        "signal",
        lambda signum, handler: handlers.append((signum, handler)),
    )

    worker_main.main()

    assert [signum for signum, _ in handlers] == [signal.SIGTERM, signal.SIGINT]
    stop = captured["stop"]
    assert isinstance(stop, Event)
    assert stop.is_set() is False
    handlers[0][1](signal.SIGTERM, None)
    assert stop.is_set() is True
    assert captured["options"]["worker_id"] == "test-host"
    assert captured["options"]["worker_version"] == worker_main.settings.worker_version
    assert captured["options"]["poll_seconds"] == worker_main.settings.worker_poll_seconds
    assert captured["options"]["batch_size"] == worker_main.settings.matches_batch_size
    # The declared copy budget reaches the worker registry, so the management
    # view reports the configured value instead of the schema default.
    assert captured["repo_options"] == {
        "max_concurrent_copies": worker_main.settings.max_concurrent_copies
    }


def test_run_forever_clears_orphan_local_copies(tmp_path):
    """A killed process must not leave multi-gigabyte copies on the temp disk."""
    work_dir = tmp_path / "work"
    orphan = work_dir / "11"
    recent = work_dir / "12"
    for directory in (orphan, recent):
        directory.mkdir(parents=True)
        (directory / "a.zip").write_bytes(b"x")
    stale = time.time() - (worker_main.ORPHAN_MAX_AGE_SECONDS + 60)
    os.utime(orphan, (stale, stale))

    stop = Event()
    repo = FakeRepo([], stop=stop)
    runtime = make_runtime(repo, FakeWriter(), tmp_path)

    runtime.run_forever(stop)

    assert not orphan.exists()
    assert recent.exists()


def test_run_once_refreshes_worker_heartbeat_while_lease_is_held(tmp_path):
    repo = FakeRepo([make_task(tmp_path)])
    runtime = make_runtime(repo, FakeWriter(), tmp_path)

    assert runtime.run_once() is True

    busy = [beat for beat in repo.worker_heartbeats if beat == ("busy", 1)]
    assert len(busy) > 1
