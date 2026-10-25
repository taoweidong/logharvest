from datetime import timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session

from src.application.services.maintenance import (
    MaintenanceRequiresPostgres,
    run_maintenance,
    sweep_workers_and_jobs,
)
from src.domain.models.entities import FileTask, Job, Worker, utcnow


def _seed(engine: Engine) -> int:
    with Session(engine) as session:
        job = Job(
            name="open", data_source_id=1, source_path="/nas", rule_set_id=1, status="running"
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        session.add(
            FileTask(
                job_id=job.id,
                source_path="/nas/a.zip",
                file_name="a.zip",
                size_bytes=1,
                status="succeeded",
            )
        )
        session.add(
            Worker(
                id="silent",
                hostname="h",
                version="0.1.0",
                status="busy",
                current_task_id=1,
                last_heartbeat=utcnow() - timedelta(seconds=300),
            )
        )
        session.commit()
        return job.id


def test_portable_sweeps_demote_workers_and_close_jobs(engine: Engine) -> None:
    """The two portable steps run for real here; SQLite can only execute these."""
    job_id = _seed(engine)

    counts = sweep_workers_and_jobs(engine)

    assert counts == {"workers_demoted": 1, "jobs_finalized": 1}
    with Session(engine) as session:
        assert session.get(Job, job_id).status == "succeeded"
        assert session.get(Worker, "silent").status == "offline"
        assert session.get(Worker, "silent").current_task_id is None

    # Repeatable: an external scheduler may call it every minute.
    assert sweep_workers_and_jobs(engine) == {"workers_demoted": 0, "jobs_finalized": 0}


def test_full_maintenance_refuses_non_postgresql(engine: Engine) -> None:
    """RECOVER_STALE_SQL is PostgreSQL-only; a partial sweep would lie about coverage."""
    with pytest.raises(MaintenanceRequiresPostgres, match="requires a PostgreSQL"):
        run_maintenance(engine)


def test_maintenance_requires_postgresql_is_not_swallowed_by_sweeps() -> None:
    assert issubclass(MaintenanceRequiresPostgres, RuntimeError)
