"""matches 补 (rule_id, created_at) 索引

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "matches"
INDEX_NAME = "ix_matches_rule_created"
COLUMNS = ["rule_id", "created_at"]


def _has_index() -> bool:
    inspector = sa.inspect(op.get_bind())
    return INDEX_NAME in {index["name"] for index in inspector.get_indexes(TABLE_NAME)}


def upgrade() -> None:
    # 0001 是按建表那一刻的 metadata 整体 create_all 出来的：新库在 0001 就已经带上
    # 这条索引，再建一次会撞 "already exists"；而 0002 之前升上来的库确实缺它。
    # 所以先问再建，让两种库升到 head 都得到同一个结果。
    if _has_index():
        return
    op.create_index(INDEX_NAME, TABLE_NAME, COLUMNS, unique=False)


def downgrade() -> None:
    if not _has_index():
        return
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
