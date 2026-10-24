import os
import shutil
import signal
import socket
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Event
from time import monotonic, time

from src.application.worker.ports import ClaimedTask
from src.application.worker.processor import LeaseLost, TaskProcessor
from src.config.settings import settings
from src.domain.rules.evaluator import CompiledRuleSet, compile_ruleset
from src.domain.rules.models import RuleSetConfig
from src.infrastructure.database import engine
from src.infrastructure.match_writer import PostgresMatchWriter
from src.infrastructure.task_queue import PostgresTaskRepository

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

    def run_once(self) -> bool:
        """Claim and process at most one task; return whether work was attempted."""
        if self._repo.is_draining(self._worker_id):
            # Keep the management view in sync without claiming new work.
            self._repo.heartbeat_worker(self._worker_id, "draining")
            return False

        task = self._repo.claim_next(self._worker_id, self._worker_version)
        if task is None:
            self._repo.heartbeat_worker(self._worker_id, "online")
            return False

        self._repo.heartbeat_worker(self._worker_id, "busy", task.id)
        try:
            try:
                processor = TaskProcessor(
                    repo=self._repo,
                    writer=self._writer,
                    rules=self._rules_factory(task),
                    work_dir=self._work_dir,
                    batch_size=self._batch_size,
                    # A task can run for minutes; keep workers.last_heartbeat
                    # fresh so the management view and the peer dead-worker
                    # sweep do not see a live process as stalled.
                    on_lease_refresh=lambda: self._repo.heartbeat_worker(
                        self._worker_id, "busy", task.id
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                # A broken rule config or work-dir setup must not leave the
                # claimed task running until stale recovery notices it.
                self._repo.complete(task.id, task.attempts, "failed", 0, 0, str(exc))
                self._finalize_job(task.job_id)
            else:
                try:
                    processor.process(task)
                except LeaseLost:
                    # Another worker owns (or recovery returned) the task now.
                    pass
                else:
                    self._finalize_job(task.job_id)
        finally:
            self._repo.heartbeat_worker(self._worker_id, "online")
        return True

    def run_forever(self, stop_event: Event | None = None) -> None:
        stop = stop_event or Event()
        self._cleanup_orphan_work_dirs()
        self._repo.register_worker(self._worker_id, socket.gethostname(), self._worker_version)
        self._recover_stale()
        self._sweep_finished_jobs()
        next_recover = monotonic() + self._recover_interval_seconds
        try:
            while not stop.is_set():
                now = monotonic()
                if now >= next_recover:
                    # Periodic sweep so abandoned tasks are retried even when no
                    # worker process restarts, and jobs closed by recovery or
                    # cancellation are finalized without a completion callback.
                    self._recover_stale()
                    self._sweep_finished_jobs()
                    next_recover = now + self._recover_interval_seconds
                if not self.run_once():
                    stop.wait(self._poll_seconds)
        finally:
            self._repo.heartbeat_worker(self._worker_id, "offline")

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
    )

    stop = Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    runtime.run_forever(stop)


if __name__ == "__main__":
    main()
