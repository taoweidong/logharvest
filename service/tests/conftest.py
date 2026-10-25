import os
import warnings

# Settings 在模块导入时就完成校验，而 jwt_secret_key 现在任何环境都必填，
# 所以测试密钥必须先落进环境，再导入任何 src 模块。
# 这里刻意用赋值而不是 setdefault：开发者 shell 里残留的 LOGHARVEST_* 会静默
# 改变（甚至打断）测试结果——签名密钥不一致会让鉴权用例成片 401。
os.environ["LOGHARVEST_ENVIRONMENT"] = "test"
os.environ["LOGHARVEST_JWT_SECRET_KEY"] = "unit-secret-key-0123456789-abcdef"

# fastapi.testclient 在导入时就发出弃用告警，早于 pytest 的 filterwarnings 生效。
warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated",
    category=UserWarning,
)

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from src.api import dependencies
from src.api.dependencies import get_engine, get_session
from src.application.services.auth import ensure_admin
from src.config.settings import settings
from src.domain import source_guard
from src.domain.models.auth import User
from src.domain.services.tokens import TokenService
from src.infrastructure.database import create_db_and_tables
from src.main import create_app

UNIT_SECRET = settings.jwt_secret_key

# 容器/开发机上的 /tmp 一律是 rw 挂载，enforce 会把每个基于 tmp_path 的作业用例
# 变成 409 —— 那与用例要测的东西毫无关系。这里只把"挂载表读不到"设为默认前提
# （判定降级为 st_mode 弱证据 -> unknown），POSIX 挂载表的匹配与三条策略由
# tests/domain/test_source_guard.py 用显式挂载表逐条覆盖。
UNREADABLE_MOUNT_TABLES = ("/proc/self/mounts-does-not-exist-in-tests",)


@pytest.fixture(autouse=True)
def _no_host_mount_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_guard, "MOUNT_TABLE_CANDIDATES", UNREADABLE_MOUNT_TABLES)


@pytest.fixture(autouse=True)
def _skip_startup_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests build their own in-memory schema; never migrate the developer database."""
    monkeypatch.setattr(settings, "auto_create_tables", False)


@pytest.fixture(autouse=True)
def _fresh_login_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """登录限流是进程级单例：失败计数串到下一个用例会让它们莫名其妙地 429。"""
    monkeypatch.setattr(dependencies, "_login_limiter", None)


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
