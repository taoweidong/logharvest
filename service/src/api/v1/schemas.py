from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from src.domain.rules.models import RuleSetConfig


class DataSourceCreate(BaseModel):
    name: str = Field(min_length=1)
    root_path: str = Field(min_length=1)
    read_only: bool = True
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


class RuleSetCreate(BaseModel):
    name: str = Field(min_length=1)
    config: RuleSetConfig


class RuleSetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    version: int
    created_at: datetime


class JobFileFilter(BaseModel):
    recursive: bool = True
    max_depth: int = Field(default=5, ge=1, le=20)
    suffixes: set[str] | None = None
    max_files: int = Field(default=20_000, ge=1)


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
    created_at: datetime
    error: str | None


class FileTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    job_id: int
    source_path: str
    file_name: str
    size_bytes: int
    status: str
    attempts: int
    processed_lines: int
    matched_rows: int
    error: str | None


class MatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    job_id: int
    rule_id: str
    rule_version: int
    source_path: str
    member_path: str
    file_name: str
    line_no: int
    raw_line: str
    fields: dict
    numeric_value: Decimal | None
    severity: str
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
