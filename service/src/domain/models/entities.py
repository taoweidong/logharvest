from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Column, Numeric, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class DataSource(SQLModel, table=True):
    __tablename__ = "data_sources"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    type: str = "mount"
    root_path: str
    read_only: bool = True
    copy_to_local: bool = True
    enabled: bool = True
    description: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class RuleSet(SQLModel, table=True):
    __tablename__ = "rule_sets"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_rule_sets_name_version"),)

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    version: int
    config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    created_by: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    data_source_id: int = Field(foreign_key="data_sources.id")
    source_path: str
    file_filter: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    rule_set_id: int = Field(foreign_key="rule_sets.id")
    status: str = "pending"
    total_files: int = 0
    total_bytes: int = 0
    cancel_requested: bool = False
    created_by: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class FileTask(SQLModel, table=True):
    __tablename__ = "file_tasks"
    __table_args__ = (
        UniqueConstraint("job_id", "source_path", name="uq_file_tasks_job_source_path"),
    )

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    source_path: str
    file_name: str
    size_bytes: int
    mtime: datetime | None = None
    status: str = "pending"
    attempts: int = 0
    max_attempts: int = 5
    scheduled_at: datetime = Field(default_factory=utcnow)
    worker_id: str | None = None
    heartbeat_at: datetime | None = None
    processed_lines: int = 0
    matched_rows: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class FileTaskAttempt(SQLModel, table=True):
    __tablename__ = "file_task_attempts"

    file_task_id: int = Field(foreign_key="file_tasks.id", primary_key=True)
    attempt_no: int = Field(primary_key=True)
    worker_id: str
    worker_version: str
    status: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    processed_lines: int = 0
    matched_rows: int = 0
    error: str | None = None


class Match(SQLModel, table=True):
    __tablename__ = "matches"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    file_task_id: int = Field(index=True)
    rule_id: str
    rule_set_id: int
    rule_version: int
    source_path: str
    member_path: str
    file_name: str
    line_no: int
    raw_line: str
    raw_truncated: bool = False
    fields: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    numeric_value: Decimal | None = Field(
        default=None,
        sa_column=Column(Numeric(38, 10), nullable=True),
    )
    severity: str = "info"
    tags: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    created_at: datetime = Field(default_factory=utcnow)


class Worker(SQLModel, table=True):
    __tablename__ = "workers"

    id: str = Field(primary_key=True)
    hostname: str
    version: str
    status: str = "online"
    drain_requested: bool = False
    current_task_id: int | None = None
    max_concurrent_copies: int = 4
    cpu_percent: float | None = None
    memory_bytes: int | None = None
    temp_disk_free_bytes: int | None = None
    last_heartbeat: datetime = Field(default_factory=utcnow)
    started_at: datetime = Field(default_factory=utcnow)
    last_error: str | None = None
