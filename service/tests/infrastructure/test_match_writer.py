from sqlalchemy import create_engine, select
from sqlmodel import Session

from src.application.worker.ports import MatchRecord
from src.domain.models.entities import Match
from src.infrastructure.match_writer import DELETE_BY_TASK_SQL, PostgresMatchWriter


def _record(task_id: int, line_no: int) -> MatchRecord:
    return MatchRecord(
        job_id=7,
        file_task_id=task_id,
        rule_id="error-rate",
        rule_set_id=3,
        rule_version=2,
        severity="error",
        tags=["errors"],
        source_path="/logs/app.tar.gz",
        member_path="app/app.log",
        file_name="app.log",
        line_no=line_no,
        raw_line="ERROR boom",
        raw_truncated=False,
        fields={"level": "ERROR"},
        numeric_value=None,
    )


def test_writer_can_be_constructed_without_connecting() -> None:
    writer = PostgresMatchWriter(create_engine("sqlite://"))
    assert writer is not None


def test_delete_sql_targets_file_task() -> None:
    assert "file_task_id" in DELETE_BY_TASK_SQL


def test_write_batch_persists_rows(engine) -> None:
    writer = PostgresMatchWriter(engine)

    writer.write_batch([_record(1, 1), _record(1, 2), _record(2, 1)])

    with Session(engine) as session:
        rows = (
            session.exec(select(Match).order_by(Match.file_task_id, Match.line_no)).scalars().all()
        )

    assert [(row.file_task_id, row.line_no) for row in rows] == [(1, 1), (1, 2), (2, 1)]
    assert rows[0].tags == ["errors"]
    assert rows[0].fields == {"level": "ERROR"}


def test_delete_for_task_only_removes_matching_rows(engine) -> None:
    writer = PostgresMatchWriter(engine)
    writer.write_batch([_record(1, 1), _record(1, 2), _record(2, 1)])

    writer.delete_for_task(1)

    with Session(engine) as session:
        rows = session.exec(select(Match)).scalars().all()
    assert [(row.file_task_id, row.line_no) for row in rows] == [(2, 1)]


def test_write_batch_with_empty_sequence_is_noop(engine) -> None:
    writer = PostgresMatchWriter(engine)

    writer.write_batch(())

    with Session(engine) as session:
        assert session.exec(select(Match)).scalars().all() == []
