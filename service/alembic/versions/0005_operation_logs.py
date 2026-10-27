"""新增 operation_logs：写操作与登录事件的审计表

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operation_logs",
        # SQLite 的 BIGINT 主键不会自增（只有 INTEGER PRIMARY KEY 才是 rowid 别名），
        # 所以沿用 matches.id 的方言变体：PostgreSQL 走 BIGSERIAL，SQLite 退化成 INTEGER。
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_name", sa.String(), nullable=False),
        sa.Column("role_hint", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        # 与 rule_sets.config 同一写法：PostgreSQL 用 JSONB，SQLite 退回 JSON。
        sa.Column(
            "detail",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("origin_ip", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        # 删号只置空 actor_id：用户名快照留在 actor_name 里，审计链不能跟着人一起消失。
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_operation_logs_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operation_logs")),
    )
    op.create_index(
        op.f("ix_operation_logs_created_at"),
        "operation_logs",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_operation_logs_actor_id"),
        "operation_logs",
        ["actor_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_operation_logs_resource"),
        "operation_logs",
        ["resource_type", "resource_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_operation_logs_resource"), table_name="operation_logs")
    op.drop_index(op.f("ix_operation_logs_actor_id"), table_name="operation_logs")
    op.drop_index(op.f("ix_operation_logs_created_at"), table_name="operation_logs")
    op.drop_table("operation_logs")
