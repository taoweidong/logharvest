from sqlalchemy import create_engine

from alembic import context
from src.config.settings import settings
from src.infrastructure.database import connect_args_for

config = context.config

# run_migrations() 会把真实连接串写入 sqlalchemy.url；直接跑 `alembic` CLI 时
# ini 里仍是占位串，此时回退到 settings.database_url（尊重 LOGHARVEST_DATABASE_URL）。
PLACEHOLDER_URL = "driver://user:pass@localhost/dbname"
configured_url = config.get_main_option("sqlalchemy.url")
if not configured_url or configured_url == PLACEHOLDER_URL:
    config.set_main_option("sqlalchemy.url", settings.database_url)

from sqlmodel import SQLModel

import src.domain.models  # noqa: F401  (register every table on SQLModel.metadata)

target_metadata = SQLModel.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # LogHarvest 的引擎是同步的（psycopg3 / SQLite），不需要 async engine from_config。
    connectable = create_engine(_url(), connect_args=connect_args_for(_url()))
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
