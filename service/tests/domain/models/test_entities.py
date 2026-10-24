from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from src.domain.models.entities import (
    DataSource,
    FileTask,
    FileTaskAttempt,
    Job,
    Match,
    RuleSet,
    Worker,
)


def make_engine():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return engine


def test_core_tables_round_trip() -> None:
    engine = make_engine()

    with Session(engine) as session:
        source = DataSource(name="nas", root_path="/data/logs")
        rules = RuleSet(name="baseline", version=1, config={"rules": []})
        session.add(source)
        session.add(rules)
        session.commit()
        session.refresh(source)
        session.refresh(rules)

        job = Job(
            name="batch-1",
            data_source_id=source.id,
            source_path="/data/logs/2026-10-24",
            rule_set_id=rules.id,
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        task = FileTask(
            job_id=job.id,
            source_path="/data/logs/2026-10-24/a.zip",
            file_name="a.zip",
            size_bytes=10,
            mtime=datetime.now(UTC),
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        attempt = FileTaskAttempt(
            file_task_id=task.id,
            attempt_no=1,
            worker_id="w1",
            worker_version="0.1.0",
            status="succeeded",
        )
        match = Match(
            job_id=job.id,
            file_task_id=task.id,
            rule_id="error",
            rule_set_id=rules.id,
            rule_version=1,
            source_path=task.source_path,
            member_path="app.log",
            file_name="app.log",
            line_no=1,
            raw_line="ERROR",
            fields={"level": "ERROR"},
        )
        worker = Worker(id="w1", hostname="host1", version="0.1.0")
        session.add(attempt)
        session.add(match)
        session.add(worker)
        session.commit()

        assert session.exec(select(Match)).one().fields == {"level": "ERROR"}
        assert session.exec(select(FileTaskAttempt)).one().attempt_no == 1
        assert session.exec(select(Worker)).one().version == "0.1.0"


def test_rule_set_name_and_version_are_unique() -> None:
    engine = make_engine()

    with Session(engine) as session:
        session.add(RuleSet(name="baseline", version=1, config={"rules": []}))
        session.commit()

        session.add(RuleSet(name="baseline", version=1, config={"rules": []}))
        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()
        assert len(session.exec(select(RuleSet)).all()) == 1


def test_file_task_job_and_source_path_are_unique() -> None:
    engine = make_engine()

    with Session(engine) as session:
        source = DataSource(name="nas", root_path="/data/logs")
        rules = RuleSet(name="baseline", version=1)
        session.add(source)
        session.add(rules)
        session.commit()
        session.refresh(source)
        session.refresh(rules)

        job = Job(
            name="batch-1",
            data_source_id=source.id,
            source_path="/data/logs",
            rule_set_id=rules.id,
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        session.add(
            FileTask(
                job_id=job.id,
                source_path="/data/logs/a.zip",
                file_name="a.zip",
                size_bytes=10,
            )
        )
        session.commit()

        session.add(
            FileTask(
                job_id=job.id,
                source_path="/data/logs/a.zip",
                file_name="a.zip",
                size_bytes=10,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()
        assert len(session.exec(select(FileTask)).all()) == 1
