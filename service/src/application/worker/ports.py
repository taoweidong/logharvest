from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
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
    encoding: str | None = None
    fallback_encoding: str | None = None
    rule_config: dict[str, Any] = field(default_factory=dict)
    # 规划时记录的源文件 size/mtime，处理器执行前用来判定永久失败。
    expected_size_bytes: int | None = None
    expected_mtime: datetime | None = None


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


@dataclass(frozen=True)
class WorkerStats:
    """Resource sample reported with worker heartbeats; absent values stay None."""

    cpu_percent: float | None = None
    memory_bytes: int | None = None
    temp_disk_free_bytes: int | None = None


class TaskRepository(Protocol):
    def touch(self, task_id: int, attempts: int) -> bool: ...

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
        retryable: bool = True,
    ) -> None: ...

    def is_job_cancelled(self, job_id: int) -> bool: ...


class MatchWriter(Protocol):
    def delete_for_task(self, task_id: int) -> None: ...

    def write_batch(self, matches: Sequence[MatchRecord]) -> None: ...
