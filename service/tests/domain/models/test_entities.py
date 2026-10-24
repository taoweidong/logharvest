from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import CreateTable, Engine, Table, inspect
from sqlalchemy.dialects import postgresql, sqlite
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
from src.infrastructure import database
from src.infrastructure.database import create_db_and_tables, create_engine_from_settings

CORE_TABLES = {
    "data_sources",
    "rule_sets",
    "jobs",
    "file_tasks",
    "file_task_attempts",
    "matches",
    "workers",
}


@pytest.fixture
def engine() -> Iterator[Engine]:
    test_engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(test_engine)
    try:
        yield test_engine
    finally:
        test_engine.dispose()


def test_create_db_and_tables_creates_seven_core_tables() -> None:
    db_engine = create_engine_from_settings("sqlite://")
    try:
        create_db_and_tables(db_engine)
        assert set(inspect(db_engine).get_table_names()) == CORE_TABLES
    finally:
        db_engine.dispose()


def test_create_db_and_tables_uses_module_engine_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_engine = create_engine("sqlite://")
    try:
        monkeypatch.setattr(database, "engine", db_engine)
        create_db_and_tables()
        assert set(inspect(db_engine).get_table_names()) == CORE_TABLES
    finally:
        db_engine.dispose()


def test_core_tables_round_trip(engine: Engine) -> None:
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
            raw_truncated=True,
            fields={"level": "ERROR"},
            numeric_value=Decimal("12.5"),
            severity="error",
            tags=["error", "critical"],
        )
        worker = Worker(id="w1", hostname="host1", version="0.1.0")
        session.add(attempt)
        session.add(match)
        session.add(worker)
        session.commit()

        stored_source = session.exec(select(DataSource)).one()
        assert stored_source.read_only is True
        assert stored_source.copy_to_local is True

        stored_task = session.exec(select(FileTask)).one()
        assert stored_task.max_attempts == 5

        stored_match = session.exec(select(Match)).one()
        assert stored_match.fields == {"level": "ERROR"}
        assert stored_match.numeric_value == Decimal("12.5")
        assert stored_match.tags == ["error", "critical"]
        assert stored_match.severity == "error"
        assert stored_match.raw_truncated is True

        assert session.exec(select(FileTaskAttempt)).one().attempt_no == 1
        assert session.exec(select(Worker)).one().version == "0.1.0"


def test_rule_set_name_and_version_are_unique(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(RuleSet(name="baseline", version=1, config={"rules": []}))
        session.commit()

        session.add(RuleSet(name="baseline", version=1, config={"rules": []}))
        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()
        assert len(session.exec(select(RuleSet)).all()) == 1


def test_rule_set_same_name_different_version_is_allowed(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(RuleSet(name="baseline", version=1, config={"rules": []}))
        session.add(RuleSet(name="baseline", version=2, config={"rules": []}))
        session.commit()

        versions = session.exec(select(RuleSet.version).order_by(RuleSet.version)).all()
        assert versions == [1, 2]


def test_file_task_job_and_source_path_are_unique(engine: Engine) -> None:
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


def test_file_task_same_source_path_under_different_job_is_allowed(engine: Engine) -> None:
    with Session(engine) as session:
        source = DataSource(name="nas", root_path="/data/logs")
        rules = RuleSet(name="baseline", version=1)
        session.add(source)
        session.add(rules)
        session.commit()
        session.refresh(source)
        session.refresh(rules)

        jobs = [
            Job(
                name="batch-1",
                data_source_id=source.id,
                source_path="/data/logs",
                rule_set_id=rules.id,
            ),
            Job(
                name="batch-2",
                data_source_id=source.id,
                source_path="/data/logs",
                rule_set_id=rules.id,
            ),
        ]
        session.add_all(jobs)
        session.commit()
        for job in jobs:
            session.refresh(job)

        session.add_all(
            [
                FileTask(
                    job_id=jobs[0].id,
                    source_path="/data/logs/a.zip",
                    file_name="a.zip",
                    size_bytes=10,
                ),
                FileTask(
                    job_id=jobs[1].id,
                    source_path="/data/logs/a.zip",
                    file_name="a.zip",
                    size_bytes=10,
                ),
            ]
        )
        session.commit()

        assert len(session.exec(select(FileTask)).all()) == 2


def _ddl(table: Table, dialect) -> str:
    return str(CreateTable(table).compile(dialect=dialect))


def test_postgresql_byte_and_memory_counters_are_bigint() -> None:
    pg = postgresql.dialect()

    assert "total_bytes BIGINT NOT NULL" in _ddl(Job.__table__, pg)
    assert "size_bytes BIGINT NOT NULL" in _ddl(FileTask.__table__, pg)

    worker_ddl = _ddl(Worker.__table__, pg)
    assert "memory_bytes BIGINT" in worker_ddl
    assert "temp_disk_free_bytes BIGINT" in worker_ddl


def test_postgresql_match_id_is_bigserial_and_sqlite_stays_autoincrement() -> None:
    assert "id BIGSERIAL NOT NULL" in _ddl(Match.__table__, postgresql.dialect())

    sqlite_ddl = _ddl(Match.__table__, sqlite.dialect())
    assert "id INTEGER NOT NULL" in sqlite_ddl
    assert "PRIMARY KEY (id)" in sqlite_ddl
    assert "BIGSERIAL" not in sqlite_ddl


def test_postgresql_json_columns_use_jsonb_and_sqlite_uses_json() -> None:
    pg = postgresql.dialect()

    assert "config JSONB NOT NULL" in _ddl(RuleSet.__table__, pg)
    assert "file_filter JSONB NOT NULL" in _ddl(Job.__table__, pg)

    match_ddl = _ddl(Match.__table__, pg)
    assert "fields JSONB NOT NULL" in match_ddl
    assert "tags JSONB NOT NULL" in match_ddl

    sqlite_ddl = _ddl(Match.__table__, sqlite.dialect())
    assert "fields JSON NOT NULL" in sqlite_ddl
    assert "tags JSON NOT NULL" in sqlite_ddl
    assert "JSONB" not in sqlite_ddl
