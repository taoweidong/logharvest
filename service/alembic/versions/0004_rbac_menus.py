"""新增 menus/role_menus，roles 补 description/is_active/is_system

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "menus",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("menu_type", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("component", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("icon", sa.String(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("show_link", sa.Boolean(), nullable=False),
        sa.Column("keep_alive", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["parent_id"], ["menus.id"], name=op.f("fk_menus_parent_id_menus")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_menus")),
    )
    op.create_index(op.f("ix_menus_parent_id"), "menus", ["parent_id"], unique=False)
    op.create_index(op.f("ix_menus_name"), "menus", ["name"], unique=True)

    op.create_table(
        "role_menus",
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("menu_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["menu_id"], ["menus.id"], name=op.f("fk_role_menus_menu_id_menus")
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_role_menus_role_id_roles")
        ),
        sa.PrimaryKeyConstraint("role_id", "menu_id", name=op.f("pk_role_menus")),
    )

    # SQLite 不支持 ALTER TABLE ADD COLUMN 之外的列变更；batch 模式在 SQLite 上
    # 重建表，在 PostgreSQL 上退化为普通 ALTER。已有角色通过 server_default 回填，
    # 因此三个 NOT NULL 列在任何一种方言上都能安全加上。
    with op.batch_alter_table("roles") as batch_op:
        batch_op.add_column(
            sa.Column("description", sa.String(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.add_column(
            sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("roles") as batch_op:
        batch_op.drop_column("is_system")
        batch_op.drop_column("is_active")
        batch_op.drop_column("description")

    op.drop_table("role_menus")
    op.drop_index(op.f("ix_menus_name"), table_name="menus")
    op.drop_index(op.f("ix_menus_parent_id"), table_name="menus")
    op.drop_table("menus")
