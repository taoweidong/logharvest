from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Column, ForeignKey, Index, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from src.domain.models.entities import utcnow


class OperationLog(SQLModel, table=True):
    """一次写操作或登录事件的一行审计留痕（只追加，无更新/删除接口）。

    ``actor_name``/``role_hint`` 是写入时的快照：``actor_id`` 在账号删除后被外键置空，
    日志仍要读得出"是谁、当时顶着哪个角色"，否则删号等于抹掉责任链。
    ``resource_id`` 统一存文本，因为 Worker 这类资源的自然主键本身就是字符串。
    """

    __tablename__ = "operation_logs"
    # 三条索引对应查询接口的三个入口：时间窗口翻页、按人追溯、按对象追溯。
    __table_args__ = (
        Index("ix_operation_logs_created_at", "created_at"),
        Index("ix_operation_logs_actor_id", "actor_id"),
        Index("ix_operation_logs_resource", "resource_type", "resource_id"),
    )

    id: int | None = Field(
        default=None,
        sa_column=Column(
            BigInteger().with_variant(Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
    )
    actor_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer(),
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    actor_name: str = ""
    role_hint: str = ""
    action: str
    resource_type: str = ""
    resource_id: str = ""
    summary: str = ""
    detail: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON().with_variant(JSONB(), "postgresql"), nullable=False),
    )
    origin_ip: str = ""
    created_at: datetime = Field(default_factory=utcnow)
