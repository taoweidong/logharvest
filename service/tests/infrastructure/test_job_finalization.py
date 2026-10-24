from sqlalchemy.engine import Engine
from sqlmodel import Session

from src.domain.models.entities import FileTask, Job
from src.infrastructure.task_queue import PostgresTaskRepository


def _seed(
    engine: Engine,
    task_statuses: list[str],
    *,
    job_status: str = "running",
    cancel_requested: bool = False,
) -> int:
    with Session(engine) as session:
        job = Job(
            name="j",
            data_source_id=1,
            source_path="/nas",
            rule_set_id=1,
            status=job_status,
            cancel_requested=cancel_requested,
        )
        session.add(job)
        session.commit()
        assert job.id is not None
        for index, status in enumerate(task_statuses, start=1):
            session.add(
                FileTask(
                    job_id=job.id,
                    source_path=f"/nas/a{index}.zip",
                    file_name=f"a{index}.zip",
                    size_bytes=10,
                    status=status,
                )
            )
        session.commit()
        return job.id


def _reload(engine: Engine, job_id: int) -> Job:
    with Session(engine) as session:
        job = session.get(Job, job_id)
    assert job is not None
    return job


def test_all_succeeded_tasks_close_the_job(engine: Engine) -> None:
    job_id = _seed(engine, ["succeeded", "succeeded"])

    assert PostgresTaskRepository(engine).finalize_job(job_id) == "succeeded"

    job = _reload(engine, job_id)
    assert job.status == "succeeded"
    assert job.finished_at is not None


def test_mixed_success_and_failure_is_partial(engine: Engine) -> None:
    job_id = _seed(engine, ["succeeded", "failed", "cancelled"])

    assert PostgresTaskRepository(engine).finalize_job(job_id) == "partial"
    assert _reload(engine, job_id).status == "partial"


def test_all_failures_fail_the_job(engine: Engine) -> None:
    job_id = _seed(engine, ["failed", "failed"])

    assert PostgresTaskRepository(engine).finalize_job(job_id) == "failed"


def test_cancel_flag_wins_over_task_outcomes(engine: Engine) -> None:
    job_id = _seed(engine, ["succeeded", "cancelled"], cancel_requested=True)

    assert PostgresTaskRepository(engine).finalize_job(job_id) == "cancelled"


def test_only_cancelled_tasks_cancel_the_job(engine: Engine) -> None:
    job_id = _seed(engine, ["cancelled", "cancelled"])

    assert PostgresTaskRepository(engine).finalize_job(job_id) == "cancelled"


def test_open_task_keeps_the_job_running(engine: Engine) -> None:
    job_id = _seed(engine, ["succeeded", "running", "pending"])

    assert PostgresTaskRepository(engine).finalize_job(job_id) is None
    assert _reload(engine, job_id).status == "running"


def test_already_terminal_job_is_not_rewritten(engine: Engine) -> None:
    job_id = _seed(engine, ["succeeded"], job_status="succeeded")

    assert PostgresTaskRepository(engine).finalize_job(job_id) is None
    assert _reload(engine, job_id).finished_at is None


def test_second_finalizer_loses_the_fence(engine: Engine) -> None:
    repo = PostgresTaskRepository(engine)
    job_id = _seed(engine, ["succeeded"])

    assert repo.finalize_job(job_id) == "succeeded"
    # A concurrent worker that reads the same snapshot must not overwrite it.
    assert repo.finalize_job(job_id) is None


def test_sweep_closes_jobs_left_by_recovery_or_cancel(engine: Engine) -> None:
    repo = PostgresTaskRepository(engine)
    closed = _seed(engine, ["succeeded", "failed"])
    open_job = _seed(engine, ["succeeded", "pending"])
    # Recovery can terminalize the last task without any completion callback,
    # and planning leaves jobs with no file tasks alone.
    empty_job = _seed(engine, [], job_status="running")

    assert repo.finalize_finished_jobs() == 1

    assert _reload(engine, closed).status == "partial"
    assert _reload(engine, open_job).status == "running"
    assert _reload(engine, empty_job).status == "running"


def test_cancel_request_closes_a_job_stuck_on_unclaimable_pending_tasks(engine: Engine) -> None:
    """CLAIM_SQL refuses a cancelled job's pending tasks, so they cannot be "open"."""
    repo = PostgresTaskRepository(engine)
    direct = _seed(engine, ["succeeded", "pending"], cancel_requested=True)
    swept = _seed(engine, ["pending"], cancel_requested=True)

    assert repo.finalize_job(direct) == "cancelled"
    assert repo.finalize_finished_jobs() == 1
    assert _reload(engine, swept).status == "cancelled"


def test_cancel_request_still_waits_for_running_tasks(engine: Engine) -> None:
    repo = PostgresTaskRepository(engine)
    job_id = _seed(engine, ["running", "pending"], cancel_requested=True)

    assert repo.finalize_job(job_id) is None
    assert repo.finalize_finished_jobs() == 0
    assert _reload(engine, job_id).status == "running"


def test_pending_task_blocks_finalization_when_job_is_not_cancelled(engine: Engine) -> None:
    job_id = _seed(engine, ["succeeded", "pending"])

    assert PostgresTaskRepository(engine).finalize_job(job_id) is None
    assert _reload(engine, job_id).status == "running"
