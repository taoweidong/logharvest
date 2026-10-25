"""M1 基线：七张业务表由 metadata 建出（尚无生产库，允许 create_all 捷径）

Revision ID: 0001
Revises:
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from sqlmodel import SQLModel

import src.domain.models  # noqa: F401
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BUSINESS_TABLES = (
    "data_sources",
    "rule_sets",
    "jobs",
    "file_tasks",
    "file_task_attempts",
    "matches",
    "workers",
)


def upgrade() -> None:
    bind = op.get_bind()
    SQLModel.metadata.create_all(
        bind,
        tables=[SQLModel.metadata.tables[name] for name in BUSINESS_TABLES],
    )


def downgrade() -> None:
    bind = op.get_bind()
    SQLModel.metadata.drop_all(
        bind,
        tables=[SQLModel.metadata.tables[name] for name in reversed(BUSINESS_TABLES)],
    )
