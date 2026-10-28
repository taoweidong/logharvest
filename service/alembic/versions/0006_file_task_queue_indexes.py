"""file_tasks 补领取/僵死回收部分索引

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "file_tasks"
CLAIM_INDEX = "ix_file_tasks_claim"
RECOVER_INDEX = "ix_file_tasks_recover"


def _has_index(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in {index["name"] for index in inspector.get_indexes(TABLE_NAME)}


def upgrade() -> None:
    # 0001 用当前 metadata create_all 建基线：新库在 0001 就已经带上这两条索引，
    # 这里只给从 0005 及更早版本升级上来的旧库补。先问再建，两条路径结果一致。
    if not _has_index(CLAIM_INDEX):
        op.create_index(
            CLAIM_INDEX,
            TABLE_NAME,
            ["scheduled_at", "id"],
            unique=False,
            postgresql_where=sa.text("status = 'pending'"),
            sqlite_where=sa.text("status = 'pending'"),
        )
    if not _has_index(RECOVER_INDEX):
        op.create_index(
            RECOVER_INDEX,
            TABLE_NAME,
            ["heartbeat_at"],
            unique=False,
            postgresql_where=sa.text("status = 'running'"),
            sqlite_where=sa.text("status = 'running'"),
        )


def downgrade() -> None:
    if _has_index(RECOVER_INDEX):
        op.drop_index(RECOVER_INDEX, table_name=TABLE_NAME)
    if _has_index(CLAIM_INDEX):
        op.drop_index(CLAIM_INDEX, table_name=TABLE_NAME)
