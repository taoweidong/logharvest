from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, create_engine

import src.domain.models  # noqa: F401  (register model tables on SQLModel.metadata)
from src.config.settings import settings

UTC_SESSION_OPTIONS = "-c timezone=UTC"


def connect_args_for(database_url: str) -> dict[str, str]:
    """Force a UTC session timezone on PostgreSQL connections.

    The timestamp columns are ``without time zone``: SQL ``now()`` stores
    wall-clock time in the session timezone, while Python writes UTC-aware
    values. Without pinning the session to UTC the two clocks disagree, and a
    non-UTC server would make every heartbeat comparison
    (``file_tasks.heartbeat_at``, stale recovery, dead-worker demotion,
    ``jobs.finished_at``) shift by the offset.
    """
    if database_url.startswith("postgresql"):
        return {"options": UTC_SESSION_OPTIONS}
    return {}


def create_engine_from_settings(database_url: str | None = None) -> Engine:
    """Build the application database engine from settings."""
    url = database_url or settings.database_url
    return create_engine(url, echo=False, connect_args=connect_args_for(url))


engine = create_engine_from_settings()


def create_db_and_tables(db_engine: Engine | None = None) -> None:
    """Create all core tables, using the public module-level engine by default.

    The module-level ``engine`` is looked up at call time so reassigning it
    (for example in tests) is honoured.
    """
    SQLModel.metadata.create_all(db_engine if db_engine is not None else engine)
