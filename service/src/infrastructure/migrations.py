from pathlib import Path

from alembic import command
from alembic import config as alembic_config

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


def run_migrations(database_url: str) -> None:
    """Upgrade the schema to head. Idempotent: a second call applies nothing."""
    cfg = alembic_config.Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
