from pathlib import Path

from sqlalchemy import text

from alembic import command
from alembic import config as alembic_config
from src.infrastructure.database import create_engine_from_settings

# 与 0001_baseline 一致；导出给测试和文档确认"启动会建哪些表"。
BUSINESS_TABLES = (
    "data_sources",
    "rule_sets",
    "jobs",
    "file_tasks",
    "file_task_attempts",
    "matches",
    "workers",
)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

# 任意但必须固定的 advisory lock 号：多个 API 副本/多个 uvicorn worker 同时启动时，
# 只有一个能执行 DDL，其余等锁。没有锁的话 0001_baseline 的 create_all 会
# 在并发下撞出 duplicate-table 错误。
ADVISORY_LOCK_KEY = 7231542


def needs_advisory_lock(database_url: str) -> bool:
    """只有 PostgreSQL 有 advisory lock；SQLite 测试路径不加锁也不建锁引擎。"""
    return database_url.startswith("postgresql")


def run_migrations(database_url: str) -> None:
    """Upgrade the schema to head. Idempotent: a second call applies nothing."""
    cfg = alembic_config.Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

    if not needs_advisory_lock(database_url):
        command.upgrade(cfg, "head")
        return

    lock_engine = create_engine_from_settings(database_url)
    try:
        with lock_engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
            try:
                command.upgrade(cfg, "head")
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY})
                conn.commit()
    finally:
        lock_engine.dispose()
