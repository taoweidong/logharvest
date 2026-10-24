from collections.abc import Sequence

from sqlalchemy import insert, text
from sqlalchemy.engine import Engine

from src.application.worker.ports import MatchRecord
from src.domain.models.entities import Match

DELETE_BY_TASK_SQL = "DELETE FROM matches WHERE file_task_id = :task_id"


class PostgresMatchWriter:
    """Bulk match persistence for the worker.

    Constructing the writer never connects; each operation opens a short
    transaction from the shared engine.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def delete_for_task(self, task_id: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(DELETE_BY_TASK_SQL), {"task_id": task_id})

    def write_batch(self, matches: Sequence[MatchRecord]) -> None:
        if not matches:
            return
        with self._engine.begin() as conn:
            conn.execute(
                insert(Match),
                [
                    {
                        "job_id": match.job_id,
                        "file_task_id": match.file_task_id,
                        "rule_id": match.rule_id,
                        "rule_set_id": match.rule_set_id,
                        "rule_version": match.rule_version,
                        "severity": match.severity,
                        "tags": match.tags,
                        "source_path": match.source_path,
                        "member_path": match.member_path,
                        "file_name": match.file_name,
                        "line_no": match.line_no,
                        "raw_line": match.raw_line,
                        "raw_truncated": match.raw_truncated,
                        "fields": match.fields,
                        "numeric_value": match.numeric_value,
                    }
                    for match in matches
                ],
            )
