from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from src.api.dependencies import get_session
from src.config.settings import settings
from src.infrastructure.database import create_db_and_tables
from src.main import create_app


@pytest.fixture()
def engine() -> Iterator[Engine]:
    db = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_db_and_tables(db)
    yield db
    db.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as db_session:
        yield db_session


@pytest.fixture()
def client(engine: Engine) -> Iterator[TestClient]:
    app = create_app()

    def override_session() -> Iterator[Session]:
        with Session(engine) as db_session:
            yield db_session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.admin_token}"}
