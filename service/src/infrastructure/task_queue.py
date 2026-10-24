from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.application.worker.ports import ClaimedTask

# Atomic claim: lock one eligible row with SKIP LOCKED, then flip it to running
# in the same transaction. ``attempts`` is incremented here so every attempt
# (including crash recovery) has a distinct attempt_no.
CLAIM_SQL = """
WITH picked AS (
    SELECT t.id
    FROM file_tasks t
    JOIN jobs j ON j.id = t.job_id
    WHERE t.status = 'pending'
      AND t.scheduled_at <= now()
      AND j.cancel_requested = false
    ORDER BY t.scheduled_at, t.id
    LIMIT 1
    FOR UPDATE OF t SKIP LOCKED
)
UPDATE file_tasks t
SET status = 'running',
    worker_id = :worker_id,
    attempts = t.attempts + 1,
    started_at = COALESCE(t.started_at, now()),
    heartbeat_at = now()
FROM picked p
WHERE t.id = p.id
RETURNING t.id, t.job_id, t.source_path, t.file_name, t.attempts;
"""

RULE_SQL = """
SELECT j.rule_set_id, r.version, r.config
FROM jobs j
JOIN rule_sets r ON r.id = j.rule_set_id
WHERE j.id = :job_id
"""

START_ATTEMPT_SQL = """
INSERT INTO file_task_attempts
    (file_task_id, attempt_no, worker_id, worker_version, status, started_at)
VALUES (:task_id, :attempt_no, :worker_id, :worker_version, 'running', now())
"""

# Heartbeat is fenced by both attempts (lease generation) and status, so a
# worker that lost its lease cannot keep a recovered/re-claimed task alive.
HEARTBEAT_SQL = """
UPDATE file_tasks
SET heartbeat_at = now(),
    processed_lines = :processed_lines,
    matched_rows = :matched_rows
WHERE id = :id
  AND attempts = :attempts
  AND status = 'running'
"""

# Heartbeat-only variant used while the source archive is copied and no new
# processed-line counters exist yet. Same lease fence as HEARTBEAT_SQL.
TOUCH_SQL = """
UPDATE file_tasks
SET heartbeat_at = now()
WHERE id = :id
  AND attempts = :attempts
  AND status = 'running'
"""

# Completion is fenced by attempts and running status as well. A failed
# attempt below max_attempts returns the task to pending with linear backoff
# and leaves finished_at unset. The attempt history row is closed in the same
# statement; when the task fence does not match, nothing is updated.
#
# The fence is repeated on the UPDATE itself, not just in the ``target`` CTE:
# a concurrent recovery may change status/attempts between the CTE snapshot
# and the row update, and READ COMMITTED re-checks the predicate once the row
# lock is acquired. This prevents a late completion from overwriting a task
# that another process already recovered.
COMPLETE_SQL = """
WITH target AS (
    SELECT id,
           attempts,
           max_attempts,
           (:status = 'failed' AND attempts < LEAST(max_attempts, :max_attempts))
               AS will_retry
    FROM file_tasks
    WHERE id = :task_id
      AND attempts = :attempts
      AND status = 'running'
), updated AS (
    UPDATE file_tasks t
    SET status = CASE WHEN target.will_retry THEN 'pending' ELSE :status END,
        scheduled_at = CASE
            WHEN target.will_retry
            THEN now() + make_interval(secs => target.attempts * 5)
            ELSE t.scheduled_at
        END,
        processed_lines = :processed_lines,
        matched_rows = :matched_rows,
        finished_at = CASE WHEN target.will_retry THEN NULL ELSE now() END,
        error = :error
    FROM target
    WHERE t.id = target.id
      AND t.status = 'running'
      AND t.attempts = :attempts
    RETURNING t.id
)
UPDATE file_task_attempts a
SET status = :status,
    finished_at = now(),
    processed_lines = :processed_lines,
    matched_rows = :matched_rows,
    error = :error
WHERE a.file_task_id = :task_id
  AND a.attempt_no = :attempts
  AND a.status = 'running'
  AND EXISTS (SELECT 1 FROM updated)
"""

# Recover tasks whose worker stopped heartbeating. Retry until the configured
# limit, otherwise fail permanently. Progress history rows still marked
# running become ``timeout``. Returns the number of recovered tasks.
#
# ``FOR UPDATE SKIP LOCKED`` makes concurrent recovery sweeps disjoint: a
# second recovery worker skips rows already locked by the first instead of
# racing it for the same stale task.
RECOVER_STALE_SQL = """
WITH stale AS (
    SELECT id, attempts, max_attempts
    FROM file_tasks
    WHERE status = 'running'
      AND (heartbeat_at IS NULL
           OR heartbeat_at < now() - make_interval(secs => :seconds))
    FOR UPDATE SKIP LOCKED
), updated AS (
    UPDATE file_tasks t
    SET status = CASE
            WHEN stale.attempts < LEAST(stale.max_attempts, :max_attempts) THEN 'pending'
            ELSE 'failed'
        END,
        scheduled_at = now(),
        error = COALESCE(t.error, 'worker heartbeat timeout')
    FROM stale
    WHERE t.id = stale.id
    RETURNING t.id
), timed_out AS (
    UPDATE file_task_attempts a
    SET status = 'timeout',
        finished_at = now()
    WHERE a.file_task_id IN (SELECT id FROM updated)
      AND a.status = 'running'
    RETURNING a.file_task_id
)
SELECT count(*) FROM updated
"""

REGISTER_WORKER_SQL = """
INSERT INTO workers (id, hostname, version, status, started_at, last_heartbeat)
VALUES (:id, :hostname, :version, 'online', now(), now())
ON CONFLICT (id) DO UPDATE
SET hostname = EXCLUDED.hostname,
    version = EXCLUDED.version,
    status = 'online',
    last_heartbeat = now()
"""

WORKER_HEARTBEAT_SQL = """
UPDATE workers
SET status = :status,
    current_task_id = :task_id,
    last_heartbeat = now()
WHERE id = :id
"""

DRAIN_SQL = "SELECT drain_requested FROM workers WHERE id = :id"


class PostgresTaskRepository:
    """PostgreSQL-backed task queue used by the worker runtime.

    The engine is only used when a method is called, so constructing the
    repository never opens a connection.
    """

    def __init__(self, engine: Engine, max_attempts: int = 5) -> None:
        self._engine = engine
        self._max_attempts = max_attempts

    def register_worker(self, worker_id: str, hostname: str, version: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(REGISTER_WORKER_SQL),
                {"id": worker_id, "hostname": hostname, "version": version},
            )

    def heartbeat_worker(
        self, worker_id: str, status: str, current_task_id: int | None = None
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(WORKER_HEARTBEAT_SQL),
                {"id": worker_id, "status": status, "task_id": current_task_id},
            )

    def is_draining(self, worker_id: str) -> bool:
        with self._engine.connect() as conn:
            value = conn.execute(
                text(DRAIN_SQL),
                {"id": worker_id},
            ).scalar_one_or_none()
        return bool(value)

    def claim_next(self, worker_id: str, worker_version: str) -> ClaimedTask | None:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    text(CLAIM_SQL),
                    {"worker_id": worker_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            rule = (
                conn.execute(
                    text(RULE_SQL),
                    {"job_id": row["job_id"]},
                )
                .mappings()
                .one()
            )
            conn.execute(
                text(START_ATTEMPT_SQL),
                {
                    "task_id": row["id"],
                    "attempt_no": row["attempts"],
                    "worker_id": worker_id,
                    "worker_version": worker_version,
                },
            )
            return ClaimedTask(
                id=row["id"],
                job_id=row["job_id"],
                source_path=Path(row["source_path"]),
                file_name=row["file_name"],
                attempts=row["attempts"],
                rule_set_id=rule["rule_set_id"],
                rule_version=rule["version"],
                rule_config=rule["config"] or {},
            )

    def heartbeat(
        self, task_id: int, attempts: int, processed_lines: int, matched_rows: int
    ) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                text(HEARTBEAT_SQL),
                {
                    "id": task_id,
                    "attempts": attempts,
                    "processed_lines": processed_lines,
                    "matched_rows": matched_rows,
                },
            )
            return result.rowcount == 1

    def touch(self, task_id: int, attempts: int) -> bool:
        """Refresh ``heartbeat_at`` without touching progress counters.

        Returns ``False`` when the lease is gone (recovered, re-claimed, or
        completed elsewhere), letting the caller stop work immediately.
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                text(TOUCH_SQL),
                {"id": task_id, "attempts": attempts},
            )
            return result.rowcount == 1

    def complete(
        self,
        task_id: int,
        attempts: int,
        status: str,
        processed_lines: int,
        matched_rows: int,
        error: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(COMPLETE_SQL),
                {
                    "task_id": task_id,
                    "attempts": attempts,
                    "status": status,
                    "processed_lines": processed_lines,
                    "matched_rows": matched_rows,
                    "error": error,
                    "max_attempts": self._max_attempts,
                },
            )

    def recover_stale(self, stale_seconds: int = 120) -> int:
        with self._engine.begin() as conn:
            recovered = conn.execute(
                text(RECOVER_STALE_SQL),
                {"seconds": stale_seconds, "max_attempts": self._max_attempts},
            ).scalar_one()
        return int(recovered)
