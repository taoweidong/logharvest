from pathlib import Path

from sqlalchemy import create_engine
from sqlmodel import Session

from src.application.worker.ports import ClaimedTask
from src.domain.models.entities import Worker
from src.infrastructure.task_queue import (
    CLAIM_SQL,
    COMPLETE_SQL,
    HEARTBEAT_SQL,
    RECOVER_STALE_SQL,
    PostgresTaskRepository,
)


def _normalized(sql: str) -> str:
    return " ".join(sql.split()).upper()


def test_claimed_task_gets_independent_rule_config_default() -> None:
    task = ClaimedTask(
        id=1,
        job_id=2,
        source_path=Path("/logs/app.tar.gz"),
        file_name="app.tar.gz",
        attempts=1,
        rule_set_id=3,
        rule_version=4,
    )
    other = ClaimedTask(
        id=5,
        job_id=6,
        source_path=Path("/logs/other.tar.gz"),
        file_name="other.tar.gz",
        attempts=1,
        rule_set_id=3,
        rule_version=4,
    )

    task.rule_config["changed"] = True

    assert other.rule_config == {}


def test_claim_sql_uses_skip_locked() -> None:
    normalized = _normalized(CLAIM_SQL)
    assert "FOR UPDATE" in normalized
    assert "SKIP LOCKED" in normalized
    assert "STATUS = 'PENDING'" in normalized


def test_claim_sql_locks_only_file_tasks_and_is_parameterized() -> None:
    normalized = _normalized(CLAIM_SQL)
    assert "FOR UPDATE OF T" in normalized
    assert ":WORKER_ID" in normalized


def test_repository_can_be_constructed_without_connecting() -> None:
    engine = create_engine("sqlite://")
    repo = PostgresTaskRepository(engine)
    assert repo is not None


def test_complete_sql_marks_finished_and_attempt_status() -> None:
    normalized = _normalized(COMPLETE_SQL)
    assert "FINISHED_AT" in normalized
    assert "FILE_TASK_ATTEMPTS" in normalized


def test_complete_sql_reschedules_retry_up_to_max_attempts() -> None:
    normalized = _normalized(COMPLETE_SQL)
    assert "ATTEMPTS < LEAST(MAX_ATTEMPTS, :MAX_ATTEMPTS)" in normalized
    assert "MAKE_INTERVAL" in normalized
    assert "THEN 'PENDING'" in normalized


def test_heartbeat_sql_is_fenced_by_lease() -> None:
    normalized = _normalized(HEARTBEAT_SQL)
    assert "ATTEMPTS = :ATTEMPTS" in normalized
    assert "STATUS = 'RUNNING'" in normalized


def test_complete_sql_repeats_the_lease_fence_on_update() -> None:
    normalized = _normalized(COMPLETE_SQL)
    assert (
        "WHERE T.ID = TARGET.ID AND T.STATUS = 'RUNNING' AND T.ATTEMPTS = :ATTEMPTS" in normalized
    )


def test_recover_stale_sql_locks_rows_with_skip_locked() -> None:
    normalized = _normalized(RECOVER_STALE_SQL)
    assert "FOR UPDATE" in normalized
    assert "SKIP LOCKED" in normalized


def test_recover_stale_sql_marks_running_attempts_timeout() -> None:
    normalized = _normalized(RECOVER_STALE_SQL)
    assert "HEARTBEAT_AT" in normalized
    assert "FILE_TASK_ATTEMPTS" in normalized
    assert "STATUS = 'TIMEOUT'" in normalized


def test_is_draining_reads_worker_flag(engine) -> None:
    with Session(engine) as session:
        session.add(Worker(id="w-drain", hostname="host", version="0.1", drain_requested=True))
        session.commit()

    repo = PostgresTaskRepository(engine)

    assert repo.is_draining("w-drain") is True
    assert repo.is_draining("missing") is False
