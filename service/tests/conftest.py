from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from src.api.dependencies import get_engine, get_session
from src.application.services.auth import ensure_admin
from src.config.settings import settings
from src.domain.models.auth import User
from src.domain.services.tokens import TokenService
from src.infrastructure.database import create_db_and_tables
from src.main import create_app

UNIT_SECRET = "unit-secret-key-0123456789-abcdef"


@pytest.fixture(autouse=True)
def _skip_startup_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests build their own in-memory schema; never migrate the developer database."""
    monkeypatch.setattr(settings, "auto_create_tables", False)


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
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def admin_user(session: Session) -> User:
    return ensure_admin(session, "admin", "Test-Admin-Pass")


@pytest.fixture()
def auth_headers(admin_user: User) -> dict[str, str]:
    token, _ = TokenService(secret_key=UNIT_SECRET).create_access_token(
        {"sub": str(admin_user.id), "username": admin_user.username}
    )
    return {"Authorization": f"Bearer {token}"}
