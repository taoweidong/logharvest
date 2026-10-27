"""接口 DTO：请求体与响应模型的唯一定义处。

历史上放在 ``src/api/v1/schemas.py``，但 ``application`` 层的四个服务
（rbac/jobs/data_sources/analytics）同样消费这些模型——依赖方向倒置成
application → api。移到本层后两个方向都合法：路由向下 import，
服务用同层模型。路由仍从这里取 ``response_model``，OpenAPI 契约不变。
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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

# GET /api/jobs?status= 的入参校验，非法值由 FastAPI 直接 422。
# 作业与文件任务共用同一套状态词表（pending/running/…/cancelled），
# 但两者是各自独立推进的状态机，所以保留独立别名而不是复用 FileTaskStatus。
JobStatusFilter = Literal[
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


class WorkerActionRead(BaseModel):
    """`POST /workers/{id}/drain|resume` 的响应：动作后的 Worker 状态快照。"""

    id: str
    drain_requested: bool
    status: str


class DrainVersionRead(BaseModel):
    """`POST /workers/drain-version` 的响应：按版本排空的计数。

    ``drained`` 是在线转 draining 的数量，``flagged`` 只是记录了标记的离线数量。
    """

    drained: int
    flagged: int


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


# --- RBAC 管理接口 ---------------------------------------------------------


class RoleBriefRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    nickname: str
    is_active: bool
    created_at: datetime
    roles: list[RoleBriefRead]


class UserCreate(BaseModel):
    # 登录是按用户名精确匹配的：允许首尾空格或不可见字符会造出看起来重复、
    # 但在登录框里几乎敲不出来的账号，所以这里限定成可输入的安全字符集。
    username: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]{1,49}$")
    password: str = Field(min_length=8, max_length=256)
    nickname: str = ""
    is_active: bool = True
    role_ids: list[int] = Field(default_factory=list)


class UserUpdate(BaseModel):
    nickname: str | None = None
    is_active: bool | None = None


class UserRolesUpdate(BaseModel):
    role_ids: list[int]


class PasswordReset(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    description: str
    is_active: bool
    is_system: bool


class RoleCreate(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,49}$")
    name: str = Field(min_length=1)
    description: str = ""
    is_active: bool = True


class RoleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        # 与 RoleCreate 的 min_length=1 对齐：整字段可选，但显式传值就不能是空白。
        if value is not None and not value.strip():
            raise ValueError("name must not be blank")
        return value


class RoleMenusUpdate(BaseModel):
    menu_ids: list[int]


class MenuRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    parent_id: int | None
    name: str
    menu_type: int
    path: str
    component: str
    title: str
    icon: str
    rank: int
    show_link: bool
    keep_alive: bool
    is_active: bool
    is_system: bool
    created_at: datetime
    children: list["MenuRead"] = Field(default_factory=list)


class MenuCreate(BaseModel):
    parent_id: int | None = None
    name: str = Field(min_length=1)
    menu_type: Literal[0, 1, 2]
    path: str = ""
    component: str = ""
    title: str = Field(min_length=1)
    icon: str = ""
    rank: int = 0
    show_link: bool = True
    keep_alive: bool = False
    is_active: bool = True


class MenuUpdate(BaseModel):
    """``model_dump(exclude_unset=True)`` 区分“未传”与“显式 null”：

    ``parent_id`` 显式传 null 表示移动到根，未传则保持原父节点。
    """

    parent_id: int | None = None
    name: str | None = None
    path: str | None = None
    component: str | None = None
    title: str | None = None
    icon: str | None = None
    rank: int | None = None
    show_link: bool | None = None
    keep_alive: bool | None = None
    is_active: bool | None = None

    @field_validator("name", "title")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        # MenuCreate 用 min_length=1 卡住了空标题/空名称，Update 的可选语义
        # （None=不改）等价于「传了就不能是空白」；path/component 的合法空串留给服务层判。
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value


MenuRead.model_rebuild()


# --- 操作日志 / 统计 / 规则索引 --------------------------------------------


class OperationLogRead(BaseModel):
    """`GET /api/operation-logs` 的一行。

    `actor_name`/`role_hint` 是写入时的快照：账号删除后 `actor_id` 置空，
    但日志仍要读得出"是谁、当时顶着哪个角色"。`detail` 只允许非敏感字段，
    口令一类永不调用方写入。
    """

    model_config = ConfigDict(from_attributes=True)
    id: int
    actor_id: int | None
    actor_name: str
    role_hint: str
    action: str
    resource_type: str
    resource_id: str
    summary: str
    detail: dict[str, Any]
    origin_ip: str
    created_at: datetime


class StatsTotalsRead(BaseModel):
    jobs: int
    succeeded: int
    partial: int
    failed: int
    cancelled: int
    running: int
    files_total: int
    bytes_total: int
    matched_rows_total: int
    file_succeeded: int
    file_failed: int


class StatsDailyRead(BaseModel):
    date: str
    jobs_created: int
    files_succeeded: int
    files_failed: int
    matched_rows: int
    bytes_total: int


class TopRuleRead(BaseModel):
    rule_id: str
    rule_version: int
    hits: int
    jobs: int


class JobStatsRead(BaseModel):
    """`GET /api/stats/jobs` 的聚合结果；日期按 UTC 日界，前端已标注。"""

    days: int
    data_source_id: int | None
    totals: StatsTotalsRead
    daily: list[StatsDailyRead]
    top_rules: list[TopRuleRead]


class RuleIndexRead(BaseModel):
    """跨版本规则索引的一行：规则仍活在 `rule_sets.config` 里，不落新表。"""

    rule_set_id: int
    rule_set_name: str
    version: int
    rule_id: str
    type: str
    severity: str
    summary: str
