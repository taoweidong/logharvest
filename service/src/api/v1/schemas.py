from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.domain.rules.models import RuleSetConfig


class DataSourceCreate(BaseModel):
    name: str = Field(min_length=1)
    root_path: str = Field(min_length=1)
    read_only: Literal[True] = True
    copy_to_local: bool = True
    description: str = ""


class DataSourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    type: str
    root_path: str
    read_only: bool
    copy_to_local: bool
    enabled: bool
    description: str
    created_at: datetime


class DataSourceTestResult(BaseModel):
    """`POST /data-sources/{id}/test` 的响应体。

    ``writability`` 是无副作用证据得出的三态判定；``read_only_confirmed`` 只有在真的
    拿到只读证据时才为真。平台判不出来（Windows/SMB）时必须是 ``unknown`` + ``false``，
    前端不能因此显示一个假的"已确认只读"绿勾。
    """

    exists: bool
    is_dir: bool
    readable: bool
    writability: Literal["read_only", "writable", "unknown"]
    read_only_confirmed: bool

    @model_validator(mode="after")
    def reject_unconfirmed_read_only(self) -> "DataSourceTestResult":
        if self.read_only_confirmed and self.writability != "read_only":
            raise ValueError("read_only_confirmed requires a read_only writability verdict")
        return self


class RuleSetCreate(BaseModel):
    name: str = Field(min_length=1)
    config: RuleSetConfig


class RuleSetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    version: int
    created_at: datetime


class RuleSetDetailRead(RuleSetRead):
    config: dict[str, Any]
    created_by: str


class JobFileFilter(BaseModel):
    recursive: bool = True
    max_depth: int = Field(default=5, ge=1, le=20)
    suffixes: set[str] | None = None
    max_files: int = Field(default=20_000, ge=1, le=20_000)


class JobCreate(BaseModel):
    name: str = Field(min_length=1)
    data_source_id: int
    source_path: str = Field(min_length=1)
    rule_set_id: int
    file_filter: JobFileFilter = Field(default_factory=JobFileFilter)


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    data_source_id: int
    source_path: str
    rule_set_id: int
    status: str
    total_files: int
    total_bytes: int
    cancel_requested: bool
    created_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


FileTaskStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "partial",
    "failed",
    "cancelled",
]


class FileTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    job_id: int
    source_path: str
    file_name: str
    size_bytes: int
    status: str
    attempts: int
    max_attempts: int
    worker_id: str | None
    scheduled_at: datetime
    heartbeat_at: datetime | None
    processed_lines: int
    matched_rows: int
    error: str | None


class FileTaskAttemptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    file_task_id: int
    attempt_no: int
    worker_id: str
    worker_version: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    processed_lines: int
    matched_rows: int
    error: str | None


class MatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    job_id: int
    file_task_id: int
    rule_id: str
    rule_version: int
    source_path: str
    member_path: str
    file_name: str
    line_no: int
    raw_line: str
    raw_truncated: bool
    fields: dict
    numeric_value: Decimal | None
    severity: str
    tags: list[str]
    created_at: datetime


class WorkerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    hostname: str
    version: str
    status: str
    drain_requested: bool
    current_task_id: int | None
    max_concurrent_copies: int
    cpu_percent: float | None
    memory_bytes: int | None
    temp_disk_free_bytes: int | None
    last_heartbeat: datetime
    started_at: datetime
    last_error: str | None


class SystemStatusRead(BaseModel):
    file_tasks: dict[str, int]
    pending_oldest_seconds: float | None
    workers_online: int


class WorkerOverviewRead(BaseModel):
    # ``online`` 只统计仍在心跳的 Worker（online/busy/draining），
    # ``by_version`` 含 offline 行，升级时用于确认旧版本是否真的清零。
    online: int
    by_version: dict[str, int]


class SystemOverviewRead(BaseModel):
    jobs: dict[str, int]
    file_tasks: dict[str, int]
    matches_24h: int
    workers: WorkerOverviewRead
    oldest_pending_seconds: int | None


class MaintenanceRunRead(BaseModel):
    recovered: int
    workers_demoted: int
    jobs_finalized: int
