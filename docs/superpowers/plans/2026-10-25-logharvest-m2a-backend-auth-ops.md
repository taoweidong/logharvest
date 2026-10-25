# LogHarvest M2a Backend Auth/Ops Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 LogHarvest 后端接上 JWT 登录与最小 RBAC、Alembic 迁移与启动接线、补齐 M1b 终审遗留的队列/Worker 语义，并新增前端需要的概览/筛选/重试/维护接口。

**Architecture:** 保持 LogHarvest 现有同步 FastAPI + SQLModel + DDD 分层，不移植基座项目的 async/classy-fastapi/Redis 缓存栈——只移植契约（JWT HS256 声明、bcrypt、`{code,message,data}` 登录响应信封）。认证只新增三张表 `users`/`roles`/`user_roles`，菜单与按钮级权限留在前端代码里。DDL 的所有权从 `create_all` 交给 Alembic（测试仍用 `create_all` 建表）。

**Tech Stack:** Python 3.12、FastAPI、SQLModel/SQLAlchemy、`python-jose[cryptography]`、`bcrypt`、`alembic`、`uv`、pytest（SQLite in-memory）、PostgreSQL（`psycopg`，本机无实例）。

**Spec:**
- [主设计方案](../specs/2026-10-24-compressed-log-extraction-design.md)（§7.2 队列生命周期、§7.3 重试/取消/幂等、§7.4 可观测性）
- [M1b 计划](./2026-10-25-logharvest-m1b-worker-queue.md)
- [M1a 计划](./2026-10-25-logharvest-m1a-control-plane-api.md)

## Global Constraints

- 所有命令在 `service/` 目录执行：`cd service && uv run --python 3.12 ...`（Python 3.12 固定，见 `service/.python-version`）。
- 提交信息用中文前缀：`feat:` / `fix:` / `docs:`。
- NAS 原始数据严格只读：只用 `rb` 打开源文件，不写、不删、不移动、不改属性；写只发生在 `LOGHARVEST_WORK_DIR` 与数据库。
- 租约栅栏：任何 `matches` 写库前必须确认 `(file_task_id, attempts, status='running')`；丢租约立即 `LeaseLost` 中止，不写终态。
- 本机没有 PostgreSQL：涉及 PG 专有 SQL 时用形状断言 + 可移植实现；能在 SQLite 真实执行的一律真实执行。
- 登录/刷新/路由三个接口返回基座信封 `{"code": 0, "message": "ok", "data": ...}`；既有业务接口保持纯 REST 对象与 `{"detail": ...}` 错误，不加信封。
- 单一管理员工具：不引入 Redis 缓存、token 黑名单、`sys_menus`/按钮权限、部门岗位、动态菜单表。
- 每个新行为都要有测试；不允许只写实现不写测试。

---

### Task 1: 认证领域核心（用户表、口令哈希、JWT）

**Files:**
- Create: `service/src/domain/models/auth.py`
- Modify: `service/src/domain/models/__init__.py`（若存在导出则追加；否则新建该文件并导出三个模型）
- Modify: `service/src/domain/models/entities.py`（不动业务表；`utcnow` 从这里复用）
- Create: `service/src/domain/services/__init__.py`（空文件）
- Create: `service/src/domain/services/passwords.py`
- Create: `service/src/domain/services/tokens.py`
- Modify: `service/src/config/settings.py`
- Modify: `service/pyproject.toml`
- Test: `service/tests/domain/services/test_passwords.py`
- Test: `service/tests/domain/services/test_tokens.py`
- Test: `service/tests/domain/models/test_auth_models.py`

**Interfaces:**
- Consumes: `src.domain.models.entities.utcnow`
- Produces:
  - `User`（表 `users`）字段：`id: int | None`、`username: str`（unique index）、`password_hash: str`、`nickname: str = ""`、`is_active: bool = True`、`created_at: datetime`
  - `Role`（表 `roles`）字段：`id: int | None`、`code: str`（unique index）、`name: str`
  - `UserRole`（表 `user_roles`）字段：`user_id: int`（PK, FK `users.id`）、`role_id: int`（PK, FK `roles.id`）
  - `hash_password(password: str) -> str` / `verify_password(password: str, hashed: str) -> bool`
  - `TokenService(secret_key: str, algorithm: str = "HS256", access_minutes: int = 30, refresh_days: int = 7)`，方法 `create_access_token(claims: dict) -> tuple[str, datetime]`、`create_refresh_token(claims: dict) -> tuple[str, datetime]`、`decode(token: str, expected_type: str) -> dict | None`
  - `ADMIN_ROLE_CODE = "admin"`
  - settings 新字段：`jwt_secret_key: str`、`access_token_expire_minutes: int = 30`、`refresh_token_expire_days: int = 7`、`admin_username: str = "admin"`、`admin_password: str = ""`

- [ ] **Step 1: 加依赖**

在 `service/pyproject.toml` 的 `dependencies` 列表末尾追加（保持既有缩进两空格）：

```toml
  "python-jose[cryptography]>=3.3.0",
  "bcrypt>=4.0.0",
  "alembic>=1.13.0",
```

Run: `cd service && uv sync --python 3.12`
Expected: 解析成功，`uv.lock` 更新。

- [ ] **Step 2: 写失败测试（口令哈希）**

`service/tests/domain/services/test_passwords.py`:

```python
from src.domain.services.passwords import hash_password, verify_password


def test_same_password_hashes_differ_and_verifies() -> None:
    first = hash_password("Correct-Horse-1")
    second = hash_password("Correct-Horse-1")

    assert first != second
    assert verify_password("Correct-Horse-1", first) is True
    assert verify_password("wrong", first) is False


def test_long_password_is_not_silently_truncated() -> None:
    # bcrypt 只取前 72 字节，长口令必须预哈希，否则尾 differences 会被忽略。
    base = "a" * 80
    variant = "a" * 79 + "b"
    hashed = hash_password(base)

    assert verify_password(base, hashed) is True
    assert verify_password(variant, hashed) is False


def test_empty_password_rejected() -> None:
    try:
        hash_password("")
    except ValueError:
        return
    raise AssertionError("empty password must be rejected")
```

Run: `cd service && uv run --python 3.12 pytest tests/domain/services/test_passwords.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'src.domain.services'`

- [ ] **Step 3: 实现口令服务**

Create `service/src/domain/services/__init__.py`（空文件）。

Create `service/src/domain/services/passwords.py`:

```python
import hashlib

import bcrypt

# bcrypt 只看前 72 字节，超长的口令必须先摘要，否则尾部差异被静默忽略。
_BCRYPT_MAX_BYTES = 72


def _digest(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX_BYTES:
        return hashlib.sha256(encoded).hexdigest().encode("utf-8")
    return encoded


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    return bcrypt.hashpw(_digest(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    if not password or not hashed:
        return False
    try:
        return bcrypt.checkpw(_digest(password), hashed.encode("utf-8"))
    except ValueError:
        return False
```

Run: `cd service && uv run --python 3.12 pytest tests/domain/services/test_passwords.py -v`
Expected: 3 passed

- [ ] **Step 4: 写失败测试（JWT）**

`service/tests/domain/services/test_tokens.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt

from src.domain.services.tokens import TokenService

SECRET = "unit-test-secret-key-0123456789-abcdef"


def make_service(**overrides) -> TokenService:
    options = {"secret_key": SECRET, "access_minutes": 30, "refresh_days": 7}
    options.update(overrides)
    return TokenService(**options)


def test_access_token_round_trip() -> None:
    service = make_service()

    token, expires_at = service.create_access_token({"sub": "1", "username": "admin"})
    claims = service.decode(token, expected_type="access")

    assert claims == {"sub": "1", "username": "admin", "type": "access"}
    assert expires_at > datetime.now(UTC)


def test_refresh_token_is_not_an_access_token() -> None:
    service = make_service()

    token, _ = service.create_refresh_token({"sub": "1"})

    assert service.decode(token, expected_type="access") is None
    assert service.decode(token, expected_type="refresh") is not None


def test_wrong_secret_and_garbage_are_rejected() -> None:
    token, _ = make_service().create_access_token({"sub": "1"})

    assert make_service(secret_key="other-secret-key-0123456789-abcdef").decode(
        token, expected_type="access"
    ) is None
    assert make_service().decode("not.a.jwt", expected_type="access") is None


def test_expired_token_is_rejected() -> None:
    service = make_service(access_minutes=-1)

    token, _ = service.create_access_token({"sub": "1"})

    assert service.decode(token, expected_type="access") is None


def test_signed_by_hand_is_still_expired_aware() -> None:
    # 直接签发一个已过期的 token，确认库层面的 exp 校验生效。
    expired = jwt.encode(
        {"sub": "1", "type": "access", "exp": datetime.now(UTC) - timedelta(seconds=1)},
        SECRET,
        algorithm="HS256",
    )

    assert make_service().decode(expired, expected_type="access") is None


def test_secret_key_shorter_than_32_chars_is_rejected() -> None:
    with pytest.raises(ValueError):
        make_service(secret_key="too-short")
```

Run: `cd service && uv run --python 3.12 pytest tests/domain/services/test_tokens.py -v`
Expected: FAIL，`ModuleNotFoundError: ... tokens`

- [ ] **Step 5: 实现 TokenService**

Create `service/src/domain/services/tokens.py`:

```python
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt

ACCESS_TYPE = "access"
REFRESH_TYPE = "refresh"
MIN_SECRET_LENGTH = 32


class TokenService:
    """HS256 access/refresh tokens with a `type` claim so the two are not interchangeable."""

    def __init__(
        self,
        secret_key: str,
        algorithm: str = "HS256",
        access_minutes: int = 30,
        refresh_days: int = 7,
    ) -> None:
        if len(secret_key) < MIN_SECRET_LENGTH:
            raise ValueError(f"jwt secret must be at least {MIN_SECRET_LENGTH} characters")
        self._secret_key = secret_key
        self._algorithm = algorithm
        self._access_delta = timedelta(minutes=access_minutes)
        self._refresh_delta = timedelta(days=refresh_days)

    def _create(self, claims: dict[str, Any], token_type: str, delta: timedelta) -> tuple[str, datetime]:
        expires_at = datetime.now(UTC) + delta
        payload = {**claims, "type": token_type, "exp": expires_at}
        return jwt.encode(payload, self._secret_key, algorithm=self._algorithm), expires_at

    def create_access_token(self, claims: dict[str, Any]) -> tuple[str, datetime]:
        return self._create(claims, ACCESS_TYPE, self._access_delta)

    def create_refresh_token(self, claims: dict[str, Any]) -> tuple[str, datetime]:
        return self._create(claims, REFRESH_TYPE, self._refresh_delta)

    def decode(self, token: str, *, expected_type: str) -> dict[str, Any] | None:
        """Return the subject claims, or None for any invalid/expired/wrong-type token."""
        try:
            payload = jwt.decode(token, self._secret_key, algorithms=[self._algorithm])
        except JWTError:
            return None
        if payload.get("type") != expected_type:
            return None
        subject = {k: v for k, v in payload.items() if k not in {"type", "exp"}}
        return subject or None
```

Run: `cd service && uv run --python 3.12 pytest tests/domain/services/test_tokens.py -v`
Expected: 7 passed

- [ ] **Step 6: 写认证模型测试并实现模型**

`service/tests/domain/models/test_auth_models.py`:

```python
import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from src.domain.models.auth import ADMIN_ROLE_CODE, Role, User, UserRole
from src.domain.services.passwords import hash_password


def test_user_role_and_link_round_trip(engine: Session) -> None:
    with Session(engine) as session:
        user = User(username="admin", password_hash=hash_password("admin123"), nickname="管理员")
        role = Role(code=ADMIN_ROLE_CODE, name="管理员")
        session.add_all([user, role])
        session.commit()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        session.commit()

        stored = session.exec(select(User).where(User.username == "admin")).one()
        links = session.exec(select(UserRole)).all()
        assert stored.is_active is True
        assert stored.created_at is not None
        assert [(link.user_id, link.role_id) for link in links] == [(stored.id, role.id)]


def test_username_is_unique(engine: Session) -> None:
    with Session(engine) as session:
        session.add(User(username="dup", password_hash="x"))
        session.commit()
        session.add(User(username="dup", password_hash="y"))
        with pytest.raises(IntegrityError):
            session.commit()
```

注意：`engine` 是 `service/tests/conftest.py` 里已有的内存 SQLite fixture（`create_db_and_tables` 建表）。

Run: `cd service && uv run --python 3.12 pytest tests/domain/models/test_auth_models.py -v`
Expected: FAIL，`ModuleNotFoundError: ... auth`

Create `service/src/domain/models/auth.py`:

```python
from datetime import datetime

from sqlmodel import Field, SQLModel

from src.domain.models.entities import utcnow

ADMIN_ROLE_CODE = "admin"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    nickname: str = ""
    is_active: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class Role(SQLModel, table=True):
    __tablename__ = "roles"

    id: int | None = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    name: str


class UserRole(SQLModel, table=True):
    __tablename__ = "user_roles"

    user_id: int = Field(foreign_key="users.id", primary_key=True)
    role_id: int = Field(foreign_key="roles.id", primary_key=True)
```

`src/infrastructure/database.py` 已经 `import src.domain.models` 以注册 metadata。若 `service/src/domain/models/__init__.py` 不存在则创建：

```python
from src.domain.models import auth, entities  # noqa: F401  (register tables on SQLModel.metadata)
```

若已存在，确认其中同时导入 `auth` 与 `entities`。

Run: `cd service && uv run --python 3.12 pytest tests/domain/models/test_auth_models.py -v`
Expected: 2 passed

- [ ] **Step 7: settings 新增 JWT/管理员配置**

Modify `service/src/config/settings.py`：在 `max_matches_per_task` 之后追加字段，并把校验器改名扩展：

```python
    jwt_secret_key: str = ""
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    admin_username: str = "admin"
    admin_password: str = ""

    @model_validator(mode="after")
    def validate_auth_settings(self) -> "Settings":
        token = self.admin_token.strip()
        if not token:
            raise ValueError("admin_token must not be empty")
        if (
            self.environment.strip().lower() not in PROTECTED_ENVIRONMENTS
            and token == DEFAULT_ADMIN_TOKEN
        ):
            raise ValueError(
                "the default admin token is only allowed when environment is dev or test"
            )
        self.admin_token = token
        # dev/test 允许空 secret（走静态 admin_token 测试路径）；其他环境必须显式配置。
        if self.jwt_secret_key.strip() and len(self.jwt_secret_key.strip()) < 32:
            raise ValueError("jwt_secret_key must be at least 32 characters")
        if self.environment.strip().lower() not in PROTECTED_ENVIRONMENTS:
            if not self.jwt_secret_key.strip():
                raise ValueError("jwt_secret_key is required outside dev and test")
            if not self.admin_password.strip():
                raise ValueError("admin_password is required outside dev and test")
        self.jwt_secret_key = self.jwt_secret_key.strip()
        return self
```

把原 `validate_admin_token` 方法整体替换为 `validate_auth_settings`（不要留两个校验器）。

Run: `cd service && uv run --python 3.12 pytest -q`
Expected: 全量仍绿（216 passed, 1 skipped）

- [ ] **Step 8: Lint 与提交**

```bash
cd service && uv run --python 3.12 ruff check src tests && uv run --python 3.12 ruff format src/domain src/config tests/domain
git add service/pyproject.toml service/uv.lock service/src/domain service/src/config/settings.py service/tests/domain
git commit -m "feat: 添加 JWT 与口令哈希认证领域核心"
```

---

### Task 2: 登录接口、认证依赖替换与管理员种子

**Files:**
- Create: `service/src/api/v1/routes_auth.py`
- Modify: `service/src/api/v1/router.py`
- Modify: `service/src/api/dependencies.py:34-53`
- Modify: `service/src/api/v1/routes_data_sources.py`、`routes_rule_sets.py`、`routes_jobs.py`、`routes_matches.py`、`routes_system.py`、`routes_workers.py`、`routes_events.py`
- Create: `service/src/application/services/auth.py`
- Create: `service/scripts/seed_admin.py`
- Modify: `service/tests/conftest.py:48-49`
- Test: `service/tests/api/test_auth.py`
- Test: `service/tests/scripts/test_seed_admin.py`

**Interfaces:**
- Consumes: Task 1 的 `User`/`Role`/`UserRole`/`ADMIN_ROLE_CODE`、`hash_password`/`verify_password`、`TokenService`；现有 `get_session`、`SessionDep`、`EngineDep`
- Produces:
  - `ok(data: Any) -> dict[str, Any]` → `{"code": 0, "message": "ok", "data": data}`（`src/api/v1/routes_auth.py`）
  - `CurrentUser` = `Annotated[User, Depends(get_current_user)]`
  - `AdminDep = Annotated[User, Depends(require_admin_user)]`（同名替换，路由侧只改 `admin.username`）
  - `POST /api/login`、`POST /api/refresh-token`、`GET /api/auth/routes`、`POST /api/logout`
  - `ensure_admin(session: Session, username: str, password: str) -> User`（幂等）

- [ ] **Step 1: 写失败测试（登录与受保护接口）**

`service/tests/api/test_auth.py`:

```python
from sqlmodel import Session, select

from src.application.services.auth import ensure_admin
from src.domain.models.auth import ADMIN_ROLE_CODE, Role, User


def test_login_returns_envelope_with_tokens_and_roles(client, session):
    ensure_admin(session, "admin", "Sup3r-Secret")

    response = client.post("/api/login", json={"username": "admin", "password": "Sup3r-Secret"})

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["username"] == "admin"
    assert data["roles"] == [ADMIN_ROLE_CODE]
    assert data["permissions"] == []
    assert data["accessToken"] and data["refreshToken"]
    assert isinstance(data["expires"], int)


def test_login_rejects_bad_password_and_unknown_user(client, session):
    ensure_admin(session, "admin", "Sup3r-Secret")

    assert client.post("/api/login", json={"username": "admin", "password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"username": "ghost", "password": "x"}).status_code == 401


def test_business_route_requires_valid_access_token(client, session, app):
    from src.api.dependencies import get_current_user
    from src.domain.services.tokens import TokenService

    ensure_admin(session, "admin", "Sup3r-Secret")
    token, _ = TokenService(secret_key="unit-secret-key-0123456789-abcdef").create_access_token(
        {"sub": "1", "username": "admin"}
    )

    # 未认证 -> 401；错误签名 -> 401；正确 token -> 200。
    assert client.get("/api/data-sources").status_code == 401
    assert client.get("/api/data-sources", headers={"Authorization": "Bearer bogus"}).status_code == 401
    assert client.get("/api/data-sources", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_refresh_token_cannot_be_used_as_access_token(client, session):
    from src.domain.services.tokens import TokenService

    user = ensure_admin(session, "admin", "Sup3r-Secret")
    service = TokenService(secret_key="unit-secret-key-0123456789-abcdef")
    refresh, _ = service.create_refresh_token({"sub": str(user.id), "username": user.username})

    assert client.get("/api/data-sources", headers={"Authorization": f"Bearer {refresh}"}).status_code == 401


def test_inactive_user_token_is_rejected(client, session):
    from src.domain.services.tokens import TokenService

    user = ensure_admin(session, "admin", "Sup3r-Secret")
    with Session(client.app.state.engine if hasattr(client.app.state, "engine") else None) as s:
        pass  # placeholder guard, replaced below in Step 3 note
```

最后一例请按 Step 3 完成 `get_current_user` 后写成：把 `user.is_active = False` 存回、再用同一 token 断言 401。实现该例时使用 `session` fixture（已有）而非 `client.app.state`：

```python
def test_inactive_user_token_is_rejected(client, session):
    from src.domain.services.tokens import TokenService

    user = ensure_admin(session, "admin", "Sup3r-Secret")
    service = TokenService(secret_key="unit-secret-key-0123456789-abcdef")
    token, _ = service.create_access_token({"sub": str(user.id), "username": user.username})

    stored = session.get(User, user.id)
    stored.is_active = False
    session.add(stored)
    session.commit()

    assert client.get("/api/data-sources", headers={"Authorization": f"Bearer {token}"}).status_code == 401
```

Run: `cd service && uv run --python 3.12 pytest tests/api/test_auth.py -v`
Expected: FAIL，`ModuleNotFoundError: ... routes_auth` / `ensure_admin`

- [ ] **Step 2: 实现认证服务与登录路由**

Create `service/src/application/services/auth.py`:

```python
from sqlmodel import Session, select

from src.domain.models.auth import ADMIN_ROLE_CODE, Role, User, UserRole
from src.domain.services.passwords import hash_password


def ensure_admin(session: Session, username: str, password: str) -> User:
    """Idempotently create the admin user plus its role link."""
    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        user = User(username=username, password_hash=hash_password(password), nickname=username)
        session.add(user)
        session.commit()
        session.refresh(user)

    role = session.exec(select(Role).where(Role.code == ADMIN_ROLE_CODE)).first()
    if role is None:
        role = Role(code=ADMIN_ROLE_CODE, name="管理员")
        session.add(role)
        session.commit()
        session.refresh(role)

    link = session.exec(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
    ).first()
    if link is None:
        session.add(UserRole(user_id=user.id, role_id=role.id))
        session.commit()
    return user


def role_codes(session: Session, user: User) -> list[str]:
    rows = session.exec(
        select(Role.code).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == user.id)
    ).all()
    return [str(code) for code in rows]
```

Create `service/src/api/v1/routes_auth.py`:

```python
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from src.application.services.auth import ensure_admin, role_codes
from src.config.settings import settings
from src.domain.models.auth import User
from src.domain.services.passwords import verify_password
from src.domain.services.tokens import TokenService
from src.api.dependencies import CurrentUser, SessionDep, get_token_service

router = APIRouter(tags=["auth"])

DEV_FALLBACK_SECRET = "unit-secret-key-0123456789-abcdef"


def ok(data: Any) -> dict[str, Any]:
    """Baseline frontend envelope: the Pure Admin store gates on ``data.code === 0``."""
    return {"code": 0, "message": "ok", "data": data}


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    refreshToken: str = Field(min_length=1)

    @field_validator("refreshToken")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("refreshToken must not be blank")
        return value


def _auth_failure() -> HTTPException:
    return HTTPException(status_code=401, detail="invalid credentials")


@router.post("/login")
def login(payload: LoginRequest, session: SessionDep):
    user = ensure_admin(session, settings.admin_username, settings.admin_password or DEV_FALLBACK_SECRET)
    # 只有配置里的管理员可以用；其他用户必须已在库里（由种子或后续管理接口创建）。
    candidate = session.exec(
        __import__("sqlmodel").select(User).where(User.username == payload.username)
    ).first()
    if candidate is None or not verify_password(payload.password, candidate.password_hash):
        raise _auth_failure()
    if not candidate.is_active:
        raise HTTPException(status_code=403, detail="user is disabled")

    service: TokenService = Depends(get_token_service)  # placeholder, see implementation note
    ...
```

实现注意（必须在写代码时落实，不要照抄上面的占位）：`login` 通过依赖注入拿 `TokenService`，签名写成

```python
@router.post("/login")
def login(
    payload: LoginRequest,
    session: SessionDep,
    service: Annotated[TokenService, Depends(get_token_service)],
):
    ...
    access_token, expires_at = service.create_access_token(
        {"sub": str(user.id), "username": user.username}
    )
    refresh_token, _ = service.create_refresh_token({"sub": str(user.id), "username": user.username})
    return ok(
        {
            "avatar": None,
            "username": user.username,
            "nickname": user.nickname,
            "roles": role_codes(session, user),
            "permissions": [],
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expires": int(expires_at.timestamp() * 1000),
        }
    )
```

并同时删掉 `ensure_admin` 在登录路径里的调用——登录只做校验，不做创建；建管理员属于 `scripts/seed_admin.py`（Step 4）。若库中还没有任何用户，登录返回 401 而不是 500。

`/refresh-token`：

```python
@router.post("/refresh-token")
def refresh(
    payload: RefreshRequest,
    session: SessionDep,
    service: Annotated[TokenService, Depends(get_token_service)],
):
    claims = service.decode(payload.refreshToken, expected_type="refresh")
    if claims is None:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    user = session.get(User, int(claims["sub"]))
    if user is None or not user.is_active or user.username != claims.get("username"):
        raise HTTPException(status_code=401, detail="invalid refresh token")
    access_token, expires_at = service.create_access_token({"sub": str(user.id), "username": user.username})
    new_refresh, _ = service.create_refresh_token({"sub": str(user.id), "username": user.username})
    return ok(
        {
            "accessToken": access_token,
            "refreshToken": new_refresh,
            "expires": int(expires_at.timestamp() * 1000),
        }
    )
```

`/auth/routes` 返回空数组（菜单在前端代码里静态定义，这是有意的最简实现）：

```python
@router.get("/auth/routes")
def auth_routes(_: CurrentUser):
    return ok([])
```

`/logout` 只回 `ok(None)`（无服务端会话；JWT 自然过期）：

```python
@router.post("/logout")
def logout(_: CurrentUser):
    return ok(None)
```

- [ ] **Step 3: 用 JWT 依赖替换静态 admin_token**

Modify `service/src/api/dependencies.py`：删除 `secrets` 导入与 `require_admin` 实现，替换为：

```python
from functools import lru_cache

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt  # not needed here; TokenService decodes
from sqlmodel import select

from src.application.services.auth import role_codes
from src.domain.models.auth import User
from src.domain.services.tokens import TokenService

_bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def get_token_service() -> TokenService:
    secret = settings.jwt_secret_key or "unit-secret-key-0123456789-abcdef"
    return TokenService(
        secret_key=secret,
        access_minutes=settings.access_token_expire_minutes,
        refresh_days=settings.refresh_token_expire_days,
    )


def _unauthorized(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=reason,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: SessionDep = None,  # noqa: ARG001 (FastAPI injects)
    service: TokenService = Depends(get_token_service),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("missing bearer token")
    claims = service.decode(credentials.credentials, expected_type="access")
    if claims is None:
        raise _unauthorized("invalid or expired token")
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id.isdigit():
        raise _unauthorized("invalid token payload")
    user = session.get(User, int(user_id))
    if user is None or not user.is_active:
        raise _unauthorized("user is not available")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin_user(
    user: CurrentUser,
    session: SessionDep = None,  # noqa: ARG001 (FastAPI injects)
) -> User:
    if ADMIN_ROLE_CODE not in role_codes(session, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return user


AdminDep = Annotated[User, Depends(require_admin_user)]
```

（实现时删掉注释里那行 `from jose import jwt`，补 `ADMIN_ROLE_CODE` 与 `Annotated` 的导入。依赖形参上的 `session: SessionDep = None` 只是签名占位，FastAPI 会注入真实 session；若 ruff 抱怨默认值，直接用 `session: SessionDep`。）

Modify 七个路由文件：把 `_: AdminDep` / `admin: AdminDep` 保留，只改**用到值**的两处：
- `service/src/api/v1/routes_jobs.py:48` 的 `admin: AdminDep` → 传给服务层的实参改为 `admin.username`
- `service/src/api/v1/routes_rule_sets.py:31` 的 `admin: AdminDep` → `created_by=admin.username`

Modify `service/src/api/v1/router.py`：`from src.api.v1 import routes_auth` 并在健康检查之后 `api_router.include_router(routes_auth.router)`（登录路由在 `/api` 前缀下，路径即 `/api/login`）。

Run: `cd service && uv run --python 3.12 pytest tests/api/test_auth.py -v`
Expected: FAIL 直到 conftest 更新；`test_login_*` 两例先绿

- [ ] **Step 4: conftest 改为签发 JWT，种子脚本可用**

Modify `service/tests/conftest.py` 的 `auth_headers`：

```python
@pytest.fixture()
def admin_user(session: Session) -> User:
    return ensure_admin(session, "admin", "Test-Admin-Pass")


@pytest.fixture()
def auth_headers(admin_user: User) -> dict[str, str]:
    token, _ = TokenService(secret_key="unit-secret-key-0123456789-abcdef").create_access_token(
        {"sub": str(admin_user.id), "username": admin_user.username}
    )
    return {"Authorization": f"Bearer {token}"}
```

（`app` fixture 不需要；如某例需要覆盖依赖，用 `app.dependency_overrides[get_current_user]`。）

Create `service/scripts/seed_admin.py`:

```python
"""Create or reset the admin account. Idempotent, safe to re-run.

Usage: cd service && uv run --python 3.12 -m scripts.seed_admin
Password comes from LOGHARVEST_ADMIN_PASSWORD; without it the command refuses.
"""

import getpass
import os
import sys

from sqlmodel import Session

from src.application.services.auth import ensure_admin
from src.config.settings import settings
from src.infrastructure.database import engine


def main() -> int:
    password = os.getenv("LOGHARVEST_ADMIN_PASSWORD") or getpass.getpass("admin password: ")
    if not password or len(password) < 8:
        print("refusing to seed: password must be at least 8 characters", file=sys.stderr)
        return 1
    with Session(engine) as session:
        user = ensure_admin(session, settings.admin_username, password)
    print(f"seeded admin user {user.username} (id={user.id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Create `service/scripts/__init__.py`（空文件）。

- [ ] **Step 5: 种子脚本测试**

`service/tests/scripts/test_seed_admin.py`:

```python
import pytest
from sqlmodel import Session

from src.application.services.auth import ensure_admin, role_codes
from src.domain.models.auth import ADMIN_ROLE_CODE, User
from src.domain.services.passwords import verify_password


def test_ensure_admin_is_idempotent(engine) -> None:
    with Session(engine) as session:
        first = ensure_admin(session, "admin", "First-Pass-1")
        second = ensure_admin(session, "admin", "Second-Pass-2")

        assert first.id == second.id
        # 幂等只保证存在，不覆盖已存在的口令。
        assert verify_password("Second-Pass-2", second.password_hash) is False
        assert verify_password("First-Pass-1", second.password_hash) is True
        assert role_codes(session, second) == [ADMIN_ROLE_CODE]
        assert session.query(User).count() == 1


def test_seed_cli_refuses_weak_password(engine, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("scripts.seed_admin.engine", engine)
    monkeypatch.setenv("LOGHARVEST_ADMIN_PASSWORD", "short")
    from scripts.seed_admin import main

    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
```

Run: `cd service && uv run --python 3.12 pytest tests/api/test_auth.py tests/scripts -v`
Expected: 全部 passed（如 `main()` 返回值而非抛 SystemExit，把断言改为 `main() == 1` 并把 `raise SystemExit(main())` 留在 `__main__` 分支）

Run: `cd service && uv run --python 3.12 pytest -q`
Expected: 全量绿（约 230 passed, 1 skipped）。若个别旧用例断言 `401 detail == "invalid admin token"`，改为断言状态码与新 detail `"missing bearer token"`，因为认证文案随机制一起更换。

- [ ] **Step 6: Lint 与提交**

```bash
cd service && uv run --python 3.12 ruff check src tests && uv run --python 3.12 ruff format src scripts tests
git add service/src/api service/src/application/services/auth.py service/scripts service/tests
git commit -m "feat: 接入 JWT 登录与管理员种子，替换静态管理员 token"
```

---

### Task 3: Alembic 迁移与启动接线

**Files:**
- Create: `service/alembic.ini`
- Create: `service/alembic/env.py`
- Create: `service/alembic/script.py.mako`
- Create: `service/alembic/versions/0001_baseline.py`
- Create: `service/alembic/versions/0002_auth_tables.py`
- Create: `service/src/infrastructure/migrations.py`
- Modify: `service/src/main.py:7-13`
- Modify: `service/tests/conftest.py`（autouse 关闭启动迁移）
- Test: `service/tests/infrastructure/test_migrations.py`

**Interfaces:**
- Consumes: `SQLModel.metadata`（Task 1 后含 10 张表）、`settings.database_url`
- Produces: `run_migrations(database_url: str) -> None`、`BUSINESS_TABLES: tuple[str, ...]`

- [ ] **Step 1: 写失败测试**

`service/tests/infrastructure/test_migrations.py`:

```python
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from src.infrastructure.migrations import BUSINESS_TABLES, run_migrations


def sqlite_url(tmp_path) -> str:
    return f"sqlite:///{(tmp_path / 'lh.db').as_posix()}"


def test_upgrade_creates_every_table_once(tmp_path) -> None:
    url = sqlite_url(tmp_path)

    run_migrations(url)
    first = set(inspect(create_engine(url)).get_table_names())
    run_migrations(url)  # 幂等：第二次不应报错或重复建表
    second = set(inspect(create_engine(url)).get_table_names())

    assert set(BUSINESS_TABLES) <= first
    assert {"users", "roles", "user_roles"} <= first
    assert "alembic_version" in first
    assert first == second


def test_migrations_match_model_metadata(tmp_path) -> None:
    url = sqlite_url(tmp_path)
    run_migrations(url)
    engine: Engine = create_engine(url)
    migrated = inspect(engine)

    from sqlmodel import SQLModel

    import src.domain.models  # noqa: F401

    for table_name in SQLModel.metadata.tables:
        assert migrated.has_table(table_name), table_name
        declared = {c.name for c in SQLModel.metadata.tables[table_name].columns}
        actual = {c["name"] for c in migrated.get_columns(table_name)}
        assert declared == actual, (table_name, declared ^ actual)
```

Run: `cd service && uv run --python 3.12 pytest tests/infrastructure/test_migrations.py -v`
Expected: FAIL，`ModuleNotFoundError: ... migrations`

- [ ] **Step 2: Alembic 脚手架**

Create `service/alembic.ini`:

```ini
[alembic]
script_location = alembic
prepend_sys_path = .
sqlalchemy.url = driver://user:pass@localhost/dbname

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console
qualname =

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

Create `service/alembic/env.py`:

```python
from alembic import context
from sqlalchemy import create_engine

from src.config.settings import settings
from src.infrastructure.database import connect_args_for

config = context.config
if config.config_file_name is not None:
    config.set_main_option("sqlalchemy.url", settings.database_url)

import src.domain.models  # noqa: E402,F401  (register every table on SQLModel.metadata)
from sqlmodel import SQLModel

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # LogHarvest 的引擎是同步的（psycopg3 / SQLite），不需要 async engine from_config。
    url = config.get_main_option("sqlalchemy.url")
    connectable = create_engine(url, connect_args=connect_args_for(url))
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

Create `service/alembic/script.py.mako`（标准模板，`alembic init` 生成的内容即可；若手写，用下面这份）:

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 3: baseline 与 auth 迁移**

Create `service/alembic/versions/0001_baseline.py`:

```python
"""M1 基线：七张业务表由 metadata 建出（尚无生产库，允许 create_all 捷径）

Revision ID: 0001
Revises:
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

import src.domain.models  # noqa: F401
from sqlmodel import SQLModel

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BUSINESS_TABLES = (
    "data_sources",
    "rule_sets",
    "jobs",
    "file_tasks",
    "file_task_attempts",
    "matches",
    "workers",
)


def upgrade() -> None:
    bind = op.get_bind()
    SQLModel.metadata.create_all(
        bind,
        tables=[SQLModel.metadata.tables[name] for name in BUSINESS_TABLES],
    )


def downgrade() -> None:
    bind = op.get_bind()
    SQLModel.metadata.drop_all(
        bind,
        tables=[SQLModel.metadata.tables[name] for name in reversed(BUSINESS_TABLES)],
    )
```

Create `service/alembic/versions/0002_auth_tables.py`（auth 表必须写显式 op，不能用 create_all 捷径，否则与 baseline 同类问题）:

```python
"""新增 users/roles/user_roles

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("nickname", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=True)
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
    )
    op.create_index(op.f("ix_roles_code"), "roles", ["code"], unique=True)
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name=op.f("fk_user_roles_role_id_roles")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_user_roles_user_id_users")),
        sa.PrimaryKeyConstraint("user_id", "role_id", name=op.f("pk_user_roles")),
    )


def downgrade() -> None:
    op.drop_table("user_roles")
    op.drop_index(op.f("ix_roles_code"), table_name="roles")
    op.drop_table("roles")
    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.drop_table("users")
```

- [ ] **Step 4: 迁移运行器与启动接线**

Create `service/src/infrastructure/migrations.py`:

```python
from pathlib import Path

from alembic import config as alembic_config
from alembic import command

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
```

Modify `service/src/main.py`:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.v1.router import api_router
from src.config.settings import settings
from src.infrastructure.migrations import run_migrations


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 迁移接线：默认开；测试用 autouse fixture 关掉，避免碰开发库。
    if settings.auto_create_tables:
        run_migrations(settings.database_url)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.include_router(api_router, prefix="/api")
    return app


app = create_app()
```

Modify `service/tests/conftest.py`，在文件里追加（放在 `engine` fixture 之前）:

```python
@pytest.fixture(autouse=True)
def _skip_startup_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests build their own in-memory schema; never migrate the developer database."""
    monkeypatch.setattr(settings, "auto_create_tables", False)
```

Run: `cd service && uv run --python 3.12 pytest tests/infrastructure/test_migrations.py -v`
Expected: 2 passed

Run: `cd service && uv run --python 3.12 pytest -q`
Expected: 全量绿

- [ ] **Step 5: 真实 CLI 走一遍**

```bash
cd service && LOGHARVEST_DATABASE_URL="sqlite:///./migrate-check.db" uv run --python 3.12 alembic upgrade head
cd service && LOGHARVEST_DATABASE_URL="sqlite:///./migrate-check.db" uv run --python 3.12 alembic current
```
Expected: 无异常；`current` 输出 `0002 (head)`。然后删除临时库：`rm -f service/migrate-check.db`（该文件是本任务自建的临时产物）。

- [ ] **Step 6: Lint 与提交**

```bash
cd service && uv run --python 3.12 ruff check src tests && uv run --python 3.12 ruff format src tests
git add service/alembic.ini service/alembic service/src/infrastructure/migrations.py service/src/main.py service/tests
git commit -m "feat: 接入 Alembic 迁移与应用启动建表"
```

---

### Task 4: 补齐队列/Worker 遗留语义（取消、永久失败、手动重试）

**Files:**
- Modify: `service/src/application/worker/ports.py`（`ClaimedTask` 新增两个字段；`TaskRepository` 新增 `is_job_cancelled`）
- Modify: `service/src/infrastructure/task_queue.py`（`CLAIM_SQL`、`COMPLETE_SQL`、`retry_failed`、`is_job_cancelled`）
- Modify: `service/src/application/worker/processor.py`（取消检查、源文件校验、不可重试失败）
- Modify: `service/src/worker/main.py`（把取消检查与作业收尾接上）
- Modify: `service/src/api/v1/routes_jobs.py` + `schemas.py`（重试接口）
- Modify: `service/src/application/services/jobs.py`
- Test: `service/tests/application/worker/test_processor.py`、`tests/infrastructure/test_task_queue.py`、`tests/worker/test_runtime.py`、`tests/api/test_jobs.py`

**Interfaces:**
- Consumes: Task 2 的 `AdminDep`（值为 `User`）
- Produces:
  - `ClaimedTask.expected_size_bytes: int | None = None`、`ClaimedTask.expected_mtime: datetime | None = None`
  - `class JobCancelled(RuntimeError)`、`class PermanentTaskFailure(RuntimeError)`（在 `processor.py`）
  - `TaskRepository.is_job_cancelled(job_id: int) -> bool`
  - `PostgresTaskRepository.complete(..., retryable: bool = True) -> None`
  - `retry_failed_tasks(session, job_id) -> int`

- [ ] **Step 1: 写失败测试（取消检查与永久失败）**

追加到 `service/tests/application/worker/test_processor.py`:

```python
def test_cancelled_job_stops_processing_and_records_cancelled(tmp_path: Path) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    cancelled = {"value": False}

    def cancel_check() -> bool:
        # 第一行之后立刻被取消：必须停止，不再评估后续行。
        cancelled["value"] = True
        return cancelled["value"]

    processor = make_processor(
        repo,
        writer,
        tmp_path / "work",
        heartbeat_every_lines=1,
        cancel_check=cancel_check,
    )

    result = processor.process(make_task(tmp_path, members={"app.log": "ERROR a\nERROR b\nERROR c\n"}))

    assert result.status == "cancelled"
    assert repo.completed == [(10, 1, "cancelled", 1, 1, None)]
    assert result.processed_lines == 1


def test_missing_source_file_fails_permanently_without_retry(tmp_path: Path) -> None:
    repo = FakeRepo()
    processor = make_processor(repo, FakeWriter(), tmp_path / "work")
    task = make_task(tmp_path)
    task.source_path.unlink()

    with pytest.raises(PermanentTaskFailure):
        processor.process(task)

    assert repo.completed == []


def test_size_change_is_permanent_failure(tmp_path: Path) -> None:
    repo = FakeRepo()
    processor = make_processor(repo, FakeWriter(), tmp_path / "work")
    task = make_task(tmp_path, expected_size_bytes=999_999)

    with pytest.raises(PermanentTaskFailure):
        processor.process(task)

    assert repo.completed == []
```

Run: `cd service && uv run --python 3.12 pytest tests/application/worker/test_processor.py -q`
Expected: FAIL（`cancel_check` 未知参数 / `PermanentTaskFailure` 未定义）

- [ ] **Step 2: 实现处理器语义**

Modify `service/src/application/worker/processor.py`：新增异常与构造参数，并在复制前后各加一道校验。

```python
class JobCancelled(RuntimeError):
    """The owning job asked to be cancelled; stop and record a cancelled task."""


class PermanentTaskFailure(RuntimeError):
    """Source changed or vanished; retrying would fail the same way, so do not retry."""
```

`__init__` 追加参数与字段：

```python
        cancel_check: Callable[[], bool] | None = None,
```
```python
        self._cancel_check = cancel_check
```

`process()` 内在 `self._verify_lease(task)` 之前插入源文件校验，并在行循环里检查取消：

```python
            self._check_source(task)
            self._verify_lease(task)
```

```python
    def _check_source(self, task: ClaimedTask) -> None:
        """Fail permanently when the archive no longer matches the plan.

        规划时记录的 size/mtime 是用户当时的意图快照；对不上的重试只会同样失败，
        还会烧光 max_attempts，所以直接判永久失败，等人工确认后手动重试。
        """
        source = task.source_path
        try:
            stat = source.stat()
        except OSError as exc:
            raise PermanentTaskFailure(f"source unavailable: {exc}") from exc
        if task.expected_size_bytes is not None and stat.st_size != task.expected_size_bytes:
            raise PermanentTaskFailure(
                f"source size changed: expected {task.expected_size_bytes}, found {stat.st_size}"
            )
        if task.expected_mtime is not None and int(stat.st_mtime) != int(task.expected_mtime.timestamp()):
            raise PermanentTaskFailure("source mtime changed since planning")
```

行循环里，在处理完每一行、心跳检查之前：

```python
                if self._cancel_check is not None and self._cancel_check():
                    raise JobCancelled(f"job {task.job_id} cancelled")
```

`except` 分支追加（放在 `except LeaseLost` 之后、通用 `except Exception` 之前）：

```python
        except JobCancelled:
            self._repo.complete(
                task.id, task.attempts, "cancelled", processed_lines, matched_rows
            )
            return TaskResult("cancelled", processed_lines, matched_rows, "job cancelled")
        except PermanentTaskFailure as exc:
            self._repo.complete(
                task.id,
                task.attempts,
                "failed",
                processed_lines,
                matched_rows,
                str(exc),
                retryable=False,
            )
            return TaskResult("failed", processed_lines, matched_rows, str(exc))
```

（`LeaseLost` 仍原样向上抛，不写终态。）

- [ ] **Step 3: 队列侧支持取消判定、不可重试与领取字段**

Modify `service/src/application/worker/ports.py`：`ClaimedTask` 追加

```python
    expected_size_bytes: int | None = None
    expected_mtime: datetime | None = None
```

（需 `from datetime import datetime`。）`TaskRepository` 协议追加

```python
    def is_job_cancelled(self, job_id: int) -> bool: ...
```

Modify `service/src/infrastructure/task_queue.py`:

`CLAIM_SQL` 的 `RETURNING` 补 `t.size_bytes, t.mtime`，并让 `claim_next` 填进 `ClaimedTask`。`COMPLETE_SQL` 的 `will_retry` 追加 `AND :retryable`：

```python
           (:status = 'failed'
            AND :retryable
            AND f.attempts < LEAST(f.max_attempts, :max_attempts)
            AND NOT j.cancel_requested) AS will_retry
```

`complete` 方法签名与参数：

```python
    def complete(
        self,
        task_id: int,
        attempts: int,
        status: str,
        processed_lines: int,
        matched_rows: int,
        error: str | None = None,
        retryable: bool = True,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(COMPLETE_SQL),
                {
                    "task_id": task_id,
                    "attempts": attempts,
                    "status": status,
                    "processed_lines": processed_lines,
                    "matched_rows": matched_rows,
                    "error": error,
                    "max_attempts": self._max_attempts,
                    "retryable": retryable,
                },
            )
```

新增取消查询（可移植，能在 SQLite 真实执行）：

```python
JOB_CANCELLED_SQL = "SELECT cancel_requested FROM jobs WHERE id = :job_id"


def is_job_cancelled(self, job_id: int) -> bool:
    with self._engine.connect() as conn:
        return bool(conn.execute(text(JOB_CANCELLED_SQL), {"job_id": job_id}).scalar_one_or_none())
```

- [ ] **Step 4: Worker 循环接线 + 手动重试接口**

Modify `service/src/worker/main.py`：给 `TaskProcessor` 传 `cancel_check`。

```python
                    cancel_check=lambda: self._repo.is_job_cancelled(task.job_id),
```

（`_repo` 若为轻量 fake 而缺方法，用与 `_recover_stale` 相同的探测：在 `run_once` 里
`cancel_check = (lambda: self._repo.is_job_cancelled(task.job_id)) if hasattr(self._repo, "is_job_cancelled") else None`。）

Modify `service/src/application/services/jobs.py`，新增：

```python
def retry_failed_tasks(session: Session, job_id: int, *, limit: int = 200) -> int:
    """Return terminal failed tasks of a job to the queue, keeping attempt history.

    attempts 不重置：手动重试是一次新尝试，仍受 max_attempts 约束，避免死循环刷库。
    """
    tasks = session.exec(
        select(FileTask)
        .where(FileTask.job_id == job_id, FileTask.status == "failed")
        .limit(limit)
    ).all()
    for task in tasks:
        task.status = "pending"
        task.error = None
        task.finished_at = None
        task.worker_id = None
        task.scheduled_at = utcnow()
        session.add(task)
    session.commit()
    return len(tasks)
```

Modify `service/src/api/v1/routes_jobs.py`，追加路由（放在 cancel 之后，风格一致）：

```python
@router.post("/{job_id}/files/retry-failed")
def retry_failed(job_id: int, session: SessionDep, admin: AdminDep):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in TERMINAL_JOB_STATUSES and job.status != "cancelled":
        raise HTTPException(status_code=409, detail="job is not retryable")
    return {"retried": retry_failed_tasks(session, job_id)}
```

- [ ] **Step 5: 测试**

追加到 `service/tests/api/test_jobs.py`:

```python
def test_retry_failed_returns_terminal_tasks_to_queue(client, auth_headers, session):
    job_id = _seed_job_with_tasks(session, [("failed", "ERROR"), ("succeeded", "OK")])

    response = client.post(f"/api/jobs/{job_id}/files/retry-failed", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"retried": 1}
    session.expire_all()
    statuses = [t.status for t in session.exec(select(FileTask).where(FileTask.job_id == job_id)).all()]
    assert sorted(statuses) == ["pending", "succeeded"]


def test_retry_failed_requires_admin(client, session):
    job_id = _seed_job_with_tasks(session, [("failed", "ERROR")])

    assert client.post(f"/api/jobs/{job_id}/files/retry-failed").status_code == 401
```

（若测试文件里还没有 `_seed_job_with_tasks`，就复用文件内已有的作业播种 helper 建两条 `FileTask`；不要在计划里另起一套 fixture。）

追加到 `service/tests/infrastructure/test_task_queue.py`:

```python
def test_complete_sql_requires_retryable_flag_for_reschedule() -> None:
    normalized = _normalized(COMPLETE_SQL)
    assert ":RETRYABLE" in normalized


def test_is_job_cancelled_reads_flag(engine) -> None:
    with Session(engine) as session:
        job = Job(
            name="j", data_source_id=1, source_path="/nas", rule_set_id=1, cancel_requested=True
        )
        session.add(job)
        session.commit()
        job_id = job.id

    repo = PostgresTaskRepository(engine)
    assert repo.is_job_cancelled(job_id) is True
    assert repo.is_job_cancelled(9999) is False
```

追加到 `service/tests/worker/test_runtime.py`（fake 需要 `is_job_cancelled` 与 `complete(retryable=...)` 形参，默认返回 False/记录参数）：

```python
def test_run_once_passes_cancel_check_and_finalizes_after_cancelled_task(tmp_path, monkeypatch):
    captured = {}

    class CancelledProcessor:
        def __init__(self, **kwargs):
            captured["cancel_check"] = kwargs["cancel_check"]

        def process(self, task):
            return TaskResult("cancelled", 1, 0, "job cancelled")

    monkeypatch.setattr(worker_main, "TaskProcessor", CancelledProcessor)
    repo = FakeRepo([make_task(tmp_path)])
    runtime = make_runtime(repo, FakeWriter(), tmp_path)

    assert runtime.run_once() is True
    assert captured["cancel_check"]() is False
    assert repo.finalized == [2]
```

Run: `cd service && uv run --python 3.12 pytest tests/application/worker tests/infrastructure/test_task_queue.py tests/worker tests/api/test_jobs.py -q`
Expected: passed

Run: `cd service && uv run --python 3.12 pytest -q`
Expected: 全量绿

- [ ] **Step 6: Lint 与提交**

```bash
cd service && uv run --python 3.12 ruff check src tests && uv run --python 3.12 ruff format src tests
git add service/src/application service/src/infrastructure/task_queue.py service/src/api service/src/worker service/tests
git commit -m "feat: 补齐取消检查、永久失败判定与失败任务手动重试"
```

---

### Task 5: 运维维护入口与前端概览接口

**Files:**
- Create: `service/src/application/services/maintenance.py`
- Modify: `service/src/api/v1/routes_system.py`
- Modify: `service/src/api/v1/schemas.py`
- Modify: `service/src/api/v1/routes_jobs.py`（文件任务按状态筛选）
- Test: `service/tests/api/test_system.py`、`service/tests/api/test_jobs.py`

**Interfaces:**
- Consumes: `PostgresTaskRepository.recover_stale/mark_dead_workers/finalize_finished_jobs`、`EngineDep`
- Produces:
  - `run_maintenance(engine: Engine, stale_seconds: int = 120) -> dict[str, int]` → `{"recovered": n, "workers_demoted": n, "jobs_finalized": n}`
  - `GET /api/system/maintenance`（POST 触发，见 Step 2 命名）
  - `GET /api/system/overview` → `{"jobs": {status: count}, "file_tasks": {status: count}, "matches_24h": int, "workers": {"online": int, "by_version": {version: count}}, "oldest_pending_seconds": int | None}`
  - `GET /api/jobs/{id}/files?status=`

- [ ] **Step 1: 写失败测试**

追加 `service/tests/api/test_system.py`:

```python
def test_overview_counts_jobs_tasks_and_workers(client, auth_headers, session, engine):
    with Session(engine) as s:
        s.add(Job(name="a", data_source_id=1, source_path="/nas", rule_set_id=1, status="running"))
        s.add(Job(name="b", data_source_id=1, source_path="/nas", rule_set_id=1, status="succeeded"))
        s.add(Worker(id="w1", hostname="h", version="0.1.0", status="online"))
        s.add(Worker(id="w2", hostname="h", version="0.1.0", status="offline"))
        s.commit()

    body = client.get("/api/system/overview", headers=auth_headers).json()

    assert body["jobs"]["running"] == 1
    assert body["jobs"]["succeeded"] == 1
    assert body["workers"]["online"] == 1
    assert body["workers"]["by_version"] == {"0.1.0": 2}
    assert body["oldest_pending_seconds"] is None


def test_maintenance_is_idempotent_and_reports_counts(client, auth_headers, engine):
    first = client.post("/api/system/maintenance", headers=auth_headers)
    second = client.post("/api/system/maintenance", headers=auth_headers)

    assert first.status_code == 200
    assert set(first.json()) == {"recovered", "workers_demoted", "jobs_finalized"}
    assert second.json()["recovered"] == 0


def test_maintenance_requires_admin(client):
    assert client.post("/api/system/maintenance").status_code == 401


def test_job_files_can_be_filtered_by_status(client, auth_headers, session):
    job_id = _seed_job_with_tasks(session, [("pending", "a"), ("succeeded", "b")])

    pending = client.get(f"/api/jobs/{job_id}/files?status=pending", headers=auth_headers).json()

    assert [item["status"] for item in pending["items"]] == ["pending"]
```

Run: `cd service && uv run --python 3.12 pytest tests/api/test_system.py -q`
Expected: FAIL（404）

- [ ] **Step 2: 维护服务**

Create `service/src/application/services/maintenance.py`:

```python
from sqlalchemy.engine import Engine

from src.infrastructure.task_queue import PostgresTaskRepository

DEFAULT_STALE_SECONDS = 120


def run_maintenance(engine: Engine, stale_seconds: int = DEFAULT_STALE_SECONDS) -> dict[str, int]:
    """Server-side compensation the worker loop cannot cover alone.

    没有存活 Worker 时（全部退出、还在升级），僵死任务、离线状态与作业收尾都
    只能由服务端触发。调度交给外部（cron / compose job），这里只保证可重复执行。
    """
    repo = PostgresTaskRepository(engine)
    return {
        "recovered": repo.recover_stale(stale_seconds),
        "workers_demoted": repo.mark_dead_workers(stale_seconds),
        "jobs_finalized": repo.finalize_finished_jobs(),
    }
```

Modify `service/src/api/v1/routes_system.py`：

```python
from src.api.dependencies import AdminDep, EngineDep
from src.application.services.maintenance import run_maintenance

router.get("/overview")(system_overview)
router.post("/maintenance")(trigger_maintenance)
```

实现（同文件内，沿用现有 `SessionDep` 风格）：

```python
@router.post("/maintenance")
def trigger_maintenance(engine: EngineDep, _: AdminDep):
    return run_maintenance(engine)


@router.get("/overview")
def system_overview(session: SessionDep, _: AdminDep):
    job_counts = {
        str(status): int(count)
        for status, count in session.exec(
            select(Job.status, func.count(Job.id)).group_by(Job.status)
        ).all()
    }
    task_counts = {
        str(status): int(count)
        for status, count in session.exec(
            select(FileTask.status, func.count(FileTask.id)).group_by(FileTask.status)
        ).all()
    }
    cutoff = utcnow() - timedelta(hours=24)
    matches_24h = int(
        session.exec(select(func.count(Match.id)).where(Match.created_at >= cutoff)).one() or 0
    )
    workers = session.exec(select(Worker)).all()
    oldest = session.exec(
        select(func.min(FileTask.scheduled_at)).where(FileTask.status == "pending")
    ).one()
    return {
        "jobs": job_counts,
        "file_tasks": task_counts,
        "matches_24h": matches_24h,
        "workers": {
            "online": sum(1 for w in workers if w.status != "offline"),
            "by_version": Counter(w.version for w in workers),
        },
        "oldest_pending_seconds": (
            int((utcnow() - oldest).total_seconds()) if oldest is not None else None
        ),
    }
```

（补 `Counter`、`timedelta`、`utcnow`、`Job`/`FileTask`/`Match`/`Worker`、`func`/`select` 的导入。）

- [ ] **Step 3: 文件任务状态筛选**

Modify `service/src/api/v1/routes_jobs.py` 的 `GET /{job_id}/files`：新增查询参数并下推到服务层。

```python
    status: Literal["pending", "running", "succeeded", "failed", "cancelled", "partial"] | None = None,
```

服务层的列表函数追加同名参数，并在查询里 `where(FileTask.status == status)`（`None` 时不加条件）。响应条目增加 `"status"` 与 `"attempts"`，供前端展示。

- [ ] **Step 4: 全量验证与提交**

```bash
cd service && uv run --python 3.12 pytest -q
cd service && uv run --python 3.12 ruff check src tests && uv run --python 3.12 ruff format src tests
git add service/src/application/services/maintenance.py service/src/api service/tests
git commit -m "feat: 新增服务端维护入口、概览统计与文件任务状态筛选"
```

---

### Task 6: 文档收口

**Files:**
- Modify: `service/README.md`
- Modify: `.env.example`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1-5 的实际接口
- Produces: 与代码一致的文档（无未实现声明）

- [ ] **Step 1: 更新 `service/README.md`**

新增/修改要点，逐条落实：
- 认证节：静态 `LOGHARVEST_ADMIN_TOKEN` 已移除；改为 `POST /api/login` 拿 `accessToken`，受保护接口带 `Authorization: Bearer <accessToken>`；`401` 含义（缺失/无效/过期）与 `403`（非 admin 角色）。
- 配置表：`LOGHARVEST_JWT_SECRET_KEY`（非 dev/test 必填，最短 32 字符）、`LOGHARVEST_ACCESS_TOKEN_EXPIRE_MINUTES=30`、`LOGHARVEST_REFRESH_TOKEN_EXPIRE_DAYS=7`、`LOGHARVEST_ADMIN_USERNAME=admin`、`LOGHARVEST_ADMIN_PASSWORD`（非 dev/test 必填）；`LOGHARVEST_AUTO_CREATE_TABLES` 现在的含义改为"启动时执行 Alembic upgrade head"。
- 迁移节：`alembic upgrade head`、`0001_baseline`/`0002_auth_tables` 的职责，以及测试仍用 `create_all`（不跑迁移）的原因。
- 管理员创建：`uv run --python 3.12 -m scripts.seed_admin`。
- API 表：`POST /api/login`、`POST /api/refresh-token`、`GET /api/auth/routes`、`POST /api/logout`、`GET /api/system/overview`、`POST /api/system/maintenance`、`POST /api/jobs/{id}/files/retry-failed`、`GET /api/jobs/{id}/files?status=`。
- Worker 节：新增取消检查（每行检查一次，作业取消即停并置 `cancelled`）、源文件 size/mtime 不符即永久失败不重试、`retryable` 栅栏；说明 `EventSource` 不能带 `Authorization` 头，前端用 `fetch` 流式读取。
- 明确不包含：前端（M2b）、真实 PostgreSQL 集成验证、按钮级权限与动态菜单、进程内并发池、`max_matches_per_task` 强制。

- [ ] **Step 2: 更新 `.env.example` 与 `README.md`**

`.env.example`：删掉 `LOGHARVEST_ADMIN_TOKEN`（若保留需注明仅供回滚测试），加入 JWT 与管理员账号变量，标注生产必填。
`README.md`（仓库根）：状态推进到 M2a，说明后端已带登录与迁移；下一步 M2b 前端。

- [ ] **Step 3: 全量验证与提交**

```bash
cd service && uv run --python 3.12 pytest -q
cd service && uv run --python 3.12 ruff check src tests
git add README.md .env.example service/README.md
git commit -m "docs: 补充 M2a 认证、迁移与运维接口说明"
```

---

## Plan Self-Review

**1. Spec coverage：**
- §7.3 取消（Worker 侧停止并置 `cancelled`）→ Task 4 Step 1-3。
- §7.3 永久失败不自动重试（大小/修改时间变化）→ Task 4 Step 2 `_check_source` + `retryable=False`。
- §7.3 手动重试 → Task 4 Step 4 `retry_failed_tasks` + 接口。
- §7.2/§7.4 Server 定时补偿兜底 → Task 5 `run_maintenance` + 端点（调度交给外部，已在文档写明）。
- 认证（基础管理员、复用 JWT/RBAC 思路）→ Task 1-2；按钮权限与动态菜单按最简原则显式不做。
- Alembic + 启动建表接线 → Task 3。
- 前端所需概览/筛选接口 → Task 5。
- 不在本计划：前端五屏（M2b）、真实 PostgreSQL 集成、进程内并发、`max_matches_per_task`、单文件级 `partial`。

**2. Placeholder scan：** Task 2 Step 2 的 `login` 代码块刻意保留了一段"实现注意"，因为该处最容易写错（依赖注入拿 `TokenService`、登录不建用户）；已给出可直接落地的最终签名，实现时必须用它替换占位段。Task 1 Step 6 与 Task 5 Step 2 同样标注了需要补齐的导入。其余步骤均为可执行内容。

**3. Type consistency：**
- `AdminDep` 在 Task 2 从 `str` 变为 `User`，两处取值点显式改为 `admin.username`；后续任务沿用 `_: AdminDep` 形态。
- `complete(..., retryable: bool = True)` 新增在末尾，不影响既有位置参数调用。
- `ClaimedTask` 新字段一律带默认值，M0/M1b 既有构造点不必修改。
- `run_maintenance(engine, stale_seconds=120)` 与仓库方法 `recover_stale/mark_dead_workers/finalize_finished_jobs` 的返回类型均为 `int`，字典三个键名与测试断言一致。
- 信封只出现在 `/api/login`、`/api/refresh-token`、`/api/auth/routes`、`/api/logout`；业务接口保持纯对象，与 M1a/M1b 既有断言不冲突。
