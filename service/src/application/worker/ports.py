from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ClaimedTask:
    id: int
    job_id: int
    source_path: Path
    file_name: str
    attempts: int
    rule_set_id: int
    rule_version: int
    encoding: str = "utf-8"
    fallback_encoding: str = "gb18030"


@dataclass(frozen=True)
class MatchRecord:
    job_id: int
    file_task_id: int
    rule_id: str
    rule_set_id: int
    rule_version: int
    severity: str
    tags: list[str]
    source_path: str
    member_path: str
    file_name: str
    line_no: int
    raw_line: str
    raw_truncated: bool
    fields: dict[str, Any]
    numeric_value: Decimal | None


@dataclass(frozen=True)
class TaskResult:
    status: str
    processed_lines: int
    matched_rows: int
    error: str | None = None


class TaskRepository(Protocol):
    def heartbeat(
        self, task_id: int, attempts: int, processed_lines: int, matched_rows: int
    ) -> bool: ...

    def complete(
        self,
        task_id: int,
        attempts: int,
        status: str,
        processed_lines: int,
        matched_rows: int,
        error: str | None = None,
    ) -> None: ...


class MatchWriter(Protocol):
    def delete_for_task(self, task_id: int) -> None: ...

    def write_batch(self, matches: Sequence[MatchRecord]) -> None: ...
