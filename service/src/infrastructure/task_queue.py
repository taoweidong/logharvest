from datetime import timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.application.worker.ports import ClaimedTask, WorkerStats
from src.domain.models.entities import utcnow

# Atomic claim: lock one eligible row with SKIP LOCKED, then flip it to running
# in the same transaction. ``attempts`` is incremented here so every attempt
# (including crash recovery) has a distinct attempt_no.
# 带上 data_sources.root_path：Worker 执行时要用它复核源文件真实路径仍在
# 数据源根下（规格 §11 的符号链接逃逸防护，见 TaskProcessor._check_root）。
# 规划期校验过的是"当时的路径字符串"，执行期必须重新按 realpath 判一次。
CLAIM_SQL = """
WITH picked AS (
    SELECT t.id, d.root_path
    FROM file_tasks t
    JOIN jobs j ON j.id = t.job_id
    LEFT JOIN data_sources d ON d.id = j.data_source_id
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
RETURNING t.id, t.job_id, t.source_path, t.file_name, t.attempts, t.size_bytes, t.mtime,
          p.root_path AS root_path;
"""

RULE_SQL = """
SELECT j.rule_set_id, r.version, r.config
FROM jobs j
JOIN rule_sets r ON r.id = j.rule_set_id
WHERE j.id = :job_id
"""

START_ATTEMPT_SQL = """
INSERT INTO file_task_attempts
    (file_task_id, attempt_no, worker_id, worker_version, status, started_at,
     processed_lines, matched_rows)
VALUES (:task_id, :attempt_no, :worker_id, :worker_version, 'running', now(), 0, 0)
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
# A failed attempt of a cancelled job is written as ``cancelled`` rather than
# back to ``pending``: CLAIM_SQL never picks up tasks of a cancelled job again,
# so requeueing would strand the job in ``running`` forever.
#
# The fence is repeated on the UPDATE itself, not just in the ``target`` CTE:
# a concurrent recovery may change status/attempts between the CTE snapshot
# and the row update, and READ COMMITTED re-checks the predicate once the row
# lock is acquired. This prevents a late completion from overwriting a task
# that another process already recovered.
COMPLETE_SQL = """
WITH target AS (
    SELECT f.id,
           f.attempts,
           f.max_attempts,
           j.cancel_requested,
           (:status = 'failed'
            AND :retryable
            AND f.attempts < LEAST(f.max_attempts, :max_attempts)
            AND NOT j.cancel_requested) AS will_retry
    FROM file_tasks f
    JOIN jobs j ON j.id = f.job_id
    WHERE f.id = :task_id
      AND f.attempts = :attempts
      AND f.status = 'running'
), updated AS (
    UPDATE file_tasks t
    SET status = CASE
            WHEN target.will_retry THEN 'pending'
            WHEN target.cancel_requested AND :status = 'failed' THEN 'cancelled'
            ELSE :status
        END,
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
# Ownership is cleared and ``finished_at`` is set when the task reaches a
# terminal state, so the management view does not keep showing a dead worker as
# the current owner. Tasks of a cancelled job are cancelled rather than
# requeued, matching CLAIM_SQL and COMPLETE_SQL.
#
# ``FOR UPDATE SKIP LOCKED`` makes concurrent recovery sweeps disjoint: a
# second recovery worker skips rows already locked by the first instead of
# racing it for the same stale task.
RECOVER_STALE_SQL = """
WITH stale AS (
    SELECT id, job_id, attempts, max_attempts
    FROM file_tasks
    WHERE status = 'running'
      AND (heartbeat_at IS NULL
           OR heartbeat_at < now() - make_interval(secs => :seconds))
    FOR UPDATE SKIP LOCKED
), updated AS (
    UPDATE file_tasks t
    SET status = CASE
            WHEN j.cancel_requested THEN 'cancelled'
            WHEN stale.attempts < LEAST(stale.max_attempts, :max_attempts) THEN 'pending'
            ELSE 'failed'
        END,
        scheduled_at = now(),
        worker_id = NULL,
        finished_at = CASE
            WHEN j.cancel_requested
                 OR stale.attempts >= LEAST(stale.max_attempts, :max_attempts)
            THEN now()
            ELSE t.finished_at
        END,
        error = COALESCE(t.error, 'worker heartbeat timeout')
    FROM stale
    JOIN jobs j ON j.id = stale.job_id
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

# 显式列出 drain_requested / processed_lines / matched_rows 这些"模型里有 Python
# 默认值、但数据库里没有 server default"的列：手写 SQL 不会经过 ORM，PostgreSQL 会
# 直接 NOT NULL 违约，而 SQLite 测试根本执行不到这些语句（见 tests/integration）。
REGISTER_WORKER_SQL = """
INSERT INTO workers (id, hostname, version, status, drain_requested, max_concurrent_copies,
                     started_at, last_heartbeat)
VALUES (:id, :hostname, :version, 'online', false, :max_concurrent_copies, now(), now())
ON CONFLICT (id) DO UPDATE
SET hostname = EXCLUDED.hostname,
    version = EXCLUDED.version,
    status = 'online',
    max_concurrent_copies = EXCLUDED.max_concurrent_copies,
    last_heartbeat = now()
"""

WORKER_HEARTBEAT_SQL = """
UPDATE workers
SET status = :status,
    current_task_id = :task_id,
    cpu_percent = COALESCE(:cpu_percent, cpu_percent),
    memory_bytes = COALESCE(:memory_bytes, memory_bytes),
    temp_disk_free_bytes = COALESCE(:temp_disk_free_bytes, temp_disk_free_bytes),
    last_heartbeat = :last_heartbeat
WHERE id = :id
"""

# A process that is SIGKILLed cannot report its own exit, so a peer's sweep
# demotes workers whose heartbeat lapsed. Portable cutoff (bound parameter) so
# the statement runs on SQLite in tests.
MARK_DEAD_WORKERS_SQL = """
UPDATE workers
SET status = 'offline',
    current_task_id = NULL
WHERE status <> 'offline'
  AND last_heartbeat < :cutoff
"""

DRAIN_SQL = "SELECT drain_requested FROM workers WHERE id = :id"

JOB_CANCELLED_SQL = "SELECT cancel_requested FROM jobs WHERE id = :job_id"

# Job finalization. The design requires a job to end once none of its file
# tasks is pending/running; these statements stay portable (no PostgreSQL-only
# syntax) so the aggregation semantics are exercised on SQLite while the queue
# itself stays PostgreSQL-only.
#
# Snapshot only when the job is still ``running``: the row-level fence in
# FINISH_JOB_SQL then decides the single writer when two workers finish the
# last tasks at the same time.
JOB_SNAPSHOT_SQL = """
SELECT j.cancel_requested AS cancel_requested,
       t.status AS task_status,
       count(*) AS task_count
FROM jobs j
JOIN file_tasks t ON t.job_id = j.id
WHERE j.id = :job_id
  AND j.status = 'running'
GROUP BY j.cancel_requested, t.status
"""

OPEN_TASK_STATUSES = ("pending", "running")
# 取消中的作业里，pending 任务永远不会被 CLAIM_SQL 领走（那里的
# ``j.cancel_requested = false``）；再把它算成"未完成"，作业与它的 SSE 就会永久
# 滞留。因此取消中只剩 running 算未完成。
CANCELLING_OPEN_TASK_STATUSES = ("running",)
FAILED_TASK_STATUSES = ("failed", "partial")


def _status_in_clause(statuses: tuple[str, ...]) -> str:
    return ", ".join(f"'{name}'" for name in statuses)


def _open_task_predicate(job_alias: str, task_alias: str = "t") -> str:
    """“这个任务还没干完”的唯一定义。

    FINISH_JOB_SQL、CANDIDATE_JOBS_SQL 与 Python 侧的 :func:`_derive_job_status`
    三处必须永远同义。各写一遍时，改一处漏一处会让两条 SQL 静默跑偏（一条提前收
    尾作业、另一条始终不收尾），而在 SQLite 与 PostgreSQL 上都不会报任何错。
    这里让它们都从同一组状态常量拼出来。
    """
    normal = f"{task_alias}.status IN ({_status_in_clause(OPEN_TASK_STATUSES)})"
    cancelling = f"{task_alias}.status IN ({_status_in_clause(CANCELLING_OPEN_TASK_STATUSES)})"
    return (
        f"(({job_alias}.cancel_requested AND {cancelling})\n"
        f"            OR (NOT {job_alias}.cancel_requested AND {normal}))"
    )


FINISH_JOB_SQL = f"""
UPDATE jobs
SET status = :status,
    finished_at = :finished_at
WHERE id = :job_id
  AND status = 'running'
  AND NOT EXISTS (
      SELECT 1
      FROM file_tasks t
      WHERE t.job_id = jobs.id
        AND {_open_task_predicate("jobs")}
  )
"""

# Compensation candidates: running jobs whose file tasks are all terminal.
# Covers work returned to failure by stale recovery, where no completion
# callback runs. A cancelled job treats ``pending`` as unreachable rather than
# open, matching _derive_job_status and CLAIM_SQL.
CANDIDATE_JOBS_SQL = f"""
SELECT j.id AS job_id
FROM jobs j
JOIN file_tasks t ON t.job_id = j.id
WHERE j.status = 'running'
GROUP BY j.id
HAVING SUM(CASE WHEN {_open_task_predicate("j")} THEN 1 ELSE 0 END) = 0
"""


def _derive_job_status(rows) -> str | None:
    """Map a job's file-task status counts onto a terminal job status.

    Returns ``None`` while any task is still open, so the caller leaves the job
    ``running``. A cancel request makes ``pending`` tasks unreachable instead of
    open: ``CLAIM_SQL`` never claims tasks of a cancelled job, so counting them
    as open would strand the job (and its SSE stream) forever.
    """
    if not rows:
        return None
    cancel_requested = bool(rows[0]["cancel_requested"])
    counts = {str(row["task_status"]): int(row["task_count"]) for row in rows}
    open_statuses = CANCELLING_OPEN_TASK_STATUSES if cancel_requested else OPEN_TASK_STATUSES
    if any(counts.get(status, 0) for status in open_statuses):
        return None

    succeeded = counts.get("succeeded", 0)
    failed = sum(counts.get(status, 0) for status in FAILED_TASK_STATUSES)
    if cancel_requested:
        return "cancelled"
    if failed and succeeded:
        return "partial"
    if failed:
        return "failed"
    if succeeded:
        return "succeeded"
    # Nothing succeeded and nothing failed: the tasks were all cancelled
    # without the job carrying the cancel flag.
    return "cancelled"


class PostgresTaskRepository:
    """PostgreSQL-backed task queue used by the worker runtime.

    The engine is only used when a method is called, so constructing the
    repository never opens a connection.
    """

    def __init__(
        self, engine: Engine, max_attempts: int = 5, max_concurrent_copies: int = 4
    ) -> None:
        self._engine = engine
        self._max_attempts = max_attempts
        self._max_concurrent_copies = max_concurrent_copies

    def register_worker(self, worker_id: str, hostname: str, version: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(REGISTER_WORKER_SQL),
                {
                    "id": worker_id,
                    "hostname": hostname,
                    "version": version,
                    "max_concurrent_copies": self._max_concurrent_copies,
                },
            )

    def heartbeat_worker(
        self,
        worker_id: str,
        status: str,
        current_task_id: int | None = None,
        stats: WorkerStats | None = None,
    ) -> None:
        values = stats or WorkerStats()
        with self._engine.begin() as conn:
            conn.execute(
                text(WORKER_HEARTBEAT_SQL),
                {
                    "id": worker_id,
                    "status": status,
                    "task_id": current_task_id,
                    "cpu_percent": values.cpu_percent,
                    "memory_bytes": values.memory_bytes,
                    "temp_disk_free_bytes": values.temp_disk_free_bytes,
                    # 绑定参数而非 now()：语句保持可移植，SQLite 测试能真实执行；
                    # 会话时区已在连接层固定为 UTC。
                    "last_heartbeat": utcnow(),
                },
            )

    def is_draining(self, worker_id: str) -> bool:
        with self._engine.connect() as conn:
            value = conn.execute(
                text(DRAIN_SQL),
                {"id": worker_id},
            ).scalar_one_or_none()
        return bool(value)

    def is_job_cancelled(self, job_id: int) -> bool:
        """Read the owning job's cancel flag so the worker can stop mid-flight."""
        with self._engine.connect() as conn:
            value = conn.execute(
                text(JOB_CANCELLED_SQL),
                {"job_id": job_id},
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
                expected_size_bytes=row["size_bytes"],
                expected_mtime=row["mtime"],
                root_path=Path(row["root_path"]) if row.get("root_path") else None,
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
        retryable: bool = True,
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
                    "retryable": retryable,
                },
            )

    def recover_stale(self, stale_seconds: int = 120) -> int:
        with self._engine.begin() as conn:
            recovered = conn.execute(
                text(RECOVER_STALE_SQL),
                {"seconds": stale_seconds, "max_attempts": self._max_attempts},
            ).scalar_one()
        return int(recovered)

    def finalize_job(self, job_id: int) -> str | None:
        """Move a running job to its terminal status, if every task is terminal.

        Called after a task completion commits, so the worker that finishes the
        last file task sees its own row and closes the job. Returns the status
        written, or ``None`` when the job stayed open or another worker won.
        """
        with self._engine.begin() as conn:
            rows = conn.execute(text(JOB_SNAPSHOT_SQL), {"job_id": job_id}).mappings().all()
            status = _derive_job_status(rows)
            if status is None:
                return None
            result = conn.execute(
                text(FINISH_JOB_SQL),
                {"job_id": job_id, "status": status, "finished_at": utcnow()},
            )
        return status if result.rowcount == 1 else None

    def finalize_finished_jobs(self) -> int:
        """Compensation sweep for jobs whose last task ended outside completion."""
        with self._engine.connect() as conn:
            job_ids = [
                int(row["job_id"])
                for row in conn.execute(text(CANDIDATE_JOBS_SQL)).mappings().all()
            ]
        return sum(1 for job_id in job_ids if self.finalize_job(job_id) is not None)

    def mark_dead_workers(self, stale_seconds: int = 120) -> int:
        """Demote workers that stopped heartbeating to ``offline``.

        Returns the number of demoted rows. A live worker refreshes its own
        heartbeat at least once per loop iteration (and every few seconds while
        processing), so it does not demote itself.
        """
        cutoff = utcnow() - timedelta(seconds=stale_seconds)
        with self._engine.begin() as conn:
            result = conn.execute(text(MARK_DEAD_WORKERS_SQL), {"cutoff": cutoff})
        return int(result.rowcount)
