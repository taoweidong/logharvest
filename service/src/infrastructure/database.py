from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, create_engine

import src.domain.models  # noqa: F401  (register model tables on SQLModel.metadata)
from src.config.settings import settings


def create_engine_from_settings(database_url: str | None = None) -> Engine:
    """Build the application database engine from settings."""
    return create_engine(database_url or settings.database_url, echo=False)


engine = create_engine_from_settings()


def create_db_and_tables(db_engine: Engine | None = None) -> None:
    """Create all core tables, using the public module-level engine by default.

    The module-level ``engine`` is looked up at call time so reassigning it
    (for example in tests) is honoured.
    """
    SQLModel.metadata.create_all(db_engine if db_engine is not None else engine)
