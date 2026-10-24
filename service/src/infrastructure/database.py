from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, create_engine

import src.domain.models  # noqa: F401  (register model tables on SQLModel.metadata)
from src.config.settings import settings


def create_engine_from_settings(database_url: str | None = None) -> Engine:
    """Build the application database engine from settings."""
    return create_engine(database_url or settings.database_url, echo=False)


_default_engine = create_engine_from_settings()
engine = _default_engine


def create_db_and_tables(engine: Engine | None = None) -> None:
    """Create all core tables, using the application engine by default."""
    SQLModel.metadata.create_all(engine if engine is not None else _default_engine)
