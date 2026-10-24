# LogHarvest M1a 控制面 API 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 M0 核心引擎之上交付可用的控制面 API：数据源、规则集版本、作业创建与 NAS 目录规划、文件任务查询、结果查询、Worker/系统状态。

**Architecture:** FastAPI 路由调用 application service，service 使用 SQLModel Session 操作 M0 的七张表。作业创建在同步请求内递归扫描 NAS 目录并写入 `file_tasks`；PostgreSQL 队列适配和真实 Worker 领取循环放在 M1b，本阶段用 SQLite + dependency override 测试 API。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLModel、SQLite（测试）、`pathspec`；`uv`。

**Spec:**
- [主设计方案](../specs/2026-10-24-compressed-log-extraction-design.md)
- [M0 实施计划](./2026-10-24-logharvest-m0-core.md)

## Global Constraints

- NAS 只读；API 只扫描、`stat` 和创建任务，不修改源文件。
- 作业目录必须位于数据源 `root_path` 下；默认递归、最大深度 5、最多 20,000 个文件。
- 文件任务只针对压缩文件；每个压缩文件一条 `file_task`。
- 规则集保存新版本，不覆盖旧版本；作业固定 `rule_set_id`。
- 所有写接口需要管理员 Bearer token；测试用 `LOGHARVEST_ADMIN_TOKEN` 默认值 `dev-admin-token`。
- API 列表强制分页；结果查询使用 `id` 游标降序。
- M1a 不实现：PostgreSQL `SKIP LOCKED` 队列、Worker 领取循环、前端、SSE、Worker 管理/升级、Alembic。
- 所有新代码必须有测试；提交信息中文，`feat/fix/docs` 前缀。
- 所有 Python 命令从 `service/` 执行：`uv run --python 3.12 ...`。

---

### Task 1: API 基础、认证、Session 与测试夹具

**Files:**
- Modify: `service/src/config/settings.py`
- Modify: `service/src/domain/models/entities.py`
- Create: `service/src/api/dependencies.py`
- Create: `service/tests/conftest.py`
- Create: `service/tests/api/test_auth.py`

**Interfaces:**
- Produces: `settings.admin_token: str`
- Produces: `get_session() -> Iterator[Session]`, `SessionDep`
- Produces: `require_admin(authorization: str | None = Header(...)) -> str`, `AdminDep`
- Produces test fixtures: `engine`, `session`, `client`, `auth_headers`
- Modifies `Job`: `idempotency_key` + unique `(created_by, idempotency_key)`

- [ ] **Step 1: Write the failing test**

`service/tests/test_auth.py` (or `tests/api/test_auth.py`):

```python
from fastapi import HTTPException
import pytest

from src.api.dependencies import require_admin


def test_require_admin_accepts_default_token() -> None:
    assert require_admin("Bearer dev-admin-token") == "admin"


def test_require_admin_rejects_missing_or_wrong_token() -> None:
    with pytest.raises(HTTPException) as missing:
        require_admin(None)
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException):
        require_admin("Bearer wrong")


def test_health_does_not_require_auth(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "logharvest"}
```

`service/tests/conftest.py`:

```python
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from src.api.dependencies import get_session
from src.main import create_app


@pytest.fixture()
def engine() -> Engine:
    db = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(db)
    yield db
    db.dispose()


@pytest.fixture()
def session(engine: Engine):
    with Session(engine) as db_session:
        yield db_session


@pytest.fixture()
def client(engine: Engine):
    app = create_app()

    def override_session():
        with Session(engine) as db_session:
            yield db_session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer dev-admin-token"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd service && uv run --python 3.12 pytest tests/api/test_auth.py -v`

Expected: FAIL because `src.api.dependencies`, `settings.admin_token`, and the fixtures do not exist.

- [ ] **Step 3: Write minimal implementation**

`service/src/config/settings.py` add:

```python
admin_token: str = "dev-admin-token"
```

`service/src/api/dependencies.py`:

```python
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlmodel import Session

from src.config.settings import settings
from src.infrastructure.database import engine


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def require_admin(
    authorization: str | None = Header(default=None),
) -> str:
    expected = f"Bearer {settings.admin_token}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin token",
        )
    return "admin"


AdminDep = Annotated[str, Depends(require_admin)]
```

`service/src/domain/models/entities.py` add to `Job`:

```python
idempotency_key: str | None = None
__table_args__ = (
    UniqueConstraint("created_by", "idempotency_key", name="uq_jobs_creator_idempotency"),
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd service && uv run --python 3.12 pytest tests/api/test_auth.py tests/test_health.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add service/src/config/settings.py service/src/api/dependencies.py service/src/domain/models/entities.py service/tests/conftest.py service/tests/api/test_auth.py
git commit -m "feat: 添加控制面 API 认证与测试夹具"
```

---

### Task 2: 数据源与规则集 API

**Files:**
- Create: `service/src/api/v1/schemas.py`
- Create: `service/src/application/services/__init__.py`
- Create: `service/src/application/services/data_sources.py`
- Create: `service/src/application/services/rule_sets.py`
- Create: `service/src/api/v1/routes_data_sources.py`
- Create: `service/src/api/v1/routes_rule_sets.py`
- Modify: `service/src/api/v1/router.py`
- Create: `service/tests/api/test_data_sources.py`
- Create: `service/tests/api/test_rule_sets.py`

**Interfaces:**
- Consumes: `SessionDep`, `AdminDep` from Task 1
- Produces: `DataSourceCreate`, `DataSourceRead`, `RuleSetCreate`, `RuleSetRead`
- Produces: `create_data_source(session, payload) -> DataSource`, `list_data_sources(session) -> list[DataSource]`
- Produces: `create_rule_set_version(session, name, config, created_by) -> RuleSet`, `list_rule_sets(session) -> list[RuleSet]`
- Produces routes: `GET/POST /api/data-sources`, `POST /api/data-sources/{id}/test`, `GET/POST /api/rule-sets`

- [ ] **Step 1: Write the failing tests**

`service/tests/api/test_data_sources.py`:

```python
def test_create_and_list_data_source(client, auth_headers, tmp_path) -> None:
    root = tmp_path / "nas"
    root.mkdir()
    created = client.post(
        "/api/data-sources",
        headers=auth_headers,
        json={"name": "nas", "root_path": str(root)},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "nas"
    assert body["read_only"] is True

    listed = client.get("/api/data-sources", headers=auth_headers)
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()] == ["nas"]


def test_data_source_path_test(client, auth_headers, tmp_path) -> None:
    root = tmp_path / "nas"
    root.mkdir()
    source = client.post(
        "/api/data-sources",
        headers=auth_headers,
        json={"name": "nas", "root_path": str(root)},
    ).json()
    response = client.post(
        f"/api/data-sources/{source['id']}/test",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"exists": True, "is_dir": True, "readable": True}


def test_data_source_requires_admin(client, tmp_path) -> None:
    response = client.post(
        "/api/data-sources",
        json={"name": "nas", "root_path": str(tmp_path)},
    )
    assert response.status_code == 401
```

`service/tests/api/test_rule_sets.py`:

```python
def make_config():
    return {
        "name": "baseline",
        "file_glob": "**/*.log",
        "rules": [
            {
                "id": "error",
                "match": {"type": "keyword", "any": ["ERROR"]},
                "severity": "error",
            }
        ],
    }


def test_create_rule_set_versions(client, auth_headers) -> None:
    first = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={"name": "baseline", "config": make_config()},
    )
    assert first.status_code == 201
    assert first.json()["version"] == 1

    second = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={"name": "baseline", "config": make_config()},
    )
    assert second.status_code == 201
    assert second.json()["version"] == 2

    listed = client.get("/api/rule-sets", headers=auth_headers)
    assert [(item["name"], item["version"]) for item in listed.json()] == [
        ("baseline", 1),
        ("baseline", 2),
    ]


def test_invalid_rule_config_is_rejected(client, auth_headers) -> None:
    response = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={"name": "bad", "config": {"name": "bad", "rules": [{"id": "x"}]}},
    )
    assert response.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd service && uv run --python 3.12 pytest tests/api/test_data_sources.py tests/api/test_rule_sets.py -v`

Expected: FAIL because routes and schemas do not exist.

- [ ] **Step 3: Write minimal implementation**

`service/src/api/v1/schemas.py`:

```python
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.domain.rules.models import RuleSetConfig


class DataSourceCreate(BaseModel):
    name: str = Field(min_length=1)
    root_path: str = Field(min_length=1)
    read_only: bool = True
    copy_to_local: bool = True
    description: str = ""


class DataSourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    type: str
    root_path: str
    read_only: bool
    copy_to_local: bool
    enabled: bool
    description: str
    created_at: datetime


class RuleSetCreate(BaseModel):
    name: str = Field(min_length=1)
    config: RuleSetConfig


class RuleSetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    version: int
    created_at: datetime
```

`service/src/application/services/data_sources.py`:

```python
from pathlib import Path

from sqlmodel import Session, select

from src.api.v1.schemas import DataSourceCreate
from src.domain.models.entities import DataSource


def create_data_source(session: Session, payload: DataSourceCreate) -> DataSource:
    source = DataSource(**payload.model_dump())
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def list_data_sources(session: Session) -> list[DataSource]:
    return list(session.exec(select(DataSource).order_by(DataSource.id)).all())


def test_data_source_path(source: DataSource) -> dict[str, bool]:
    path = Path(source.root_path)
    return {
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "readable": path.is_dir() and path.stat().st_mode & 0o444 != 0,
    }
```

`service/src/application/services/rule_sets.py`:

```python
from sqlalchemy import func
from sqlmodel import Session, select

from src.domain.models.entities import RuleSet
from src.domain.rules.models import RuleSetConfig


def create_rule_set_version(
    session: Session,
    name: str,
    config: RuleSetConfig,
    created_by: str,
) -> RuleSet:
    max_version = session.exec(
        select(func.max(RuleSet.version)).where(RuleSet.name == name)
    ).one()
    version = (max_version or 0) + 1
    rule_set = RuleSet(
        name=name,
        version=version,
        config=config.model_dump(mode="json"),
        created_by=created_by,
    )
    session.add(rule_set)
    session.commit()
    session.refresh(rule_set)
    return rule_set


def list_rule_sets(session: Session) -> list[RuleSet]:
    return list(session.exec(select(RuleSet).order_by(RuleSet.name, RuleSet.version)).all())
```

Routes:

`routes_data_sources.py`:

```python
from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError

from src.api.dependencies import AdminDep, SessionDep
from src.api.v1.schemas import DataSourceCreate, DataSourceRead
from src.application.services.data_sources import (
    create_data_source,
    list_data_sources,
    test_data_source_path,
)

router = APIRouter(prefix="/data-sources", tags=["data-sources"])


@router.get("", response_model=list[DataSourceRead])
def get_data_sources(session: SessionDep, _: AdminDep):
    return list_data_sources(session)


@router.post("", response_model=DataSourceRead, status_code=201)
def post_data_source(payload: DataSourceCreate, session: SessionDep, _: AdminDep):
    try:
        return create_data_source(session, payload)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="data source name already exists") from exc


@router.post("/{source_id}/test")
def test_source(source_id: int, session: SessionDep, _: AdminDep):
    from src.domain.models.entities import DataSource

    source = session.get(DataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="data source not found")
    return test_data_source_path(source)
```

`routes_rule_sets.py`:

```python
from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError

from src.api.dependencies import AdminDep, SessionDep
from src.api.v1.schemas import RuleSetCreate, RuleSetRead
from src.application.services.rule_sets import create_rule_set_version, list_rule_sets

router = APIRouter(prefix="/rule-sets", tags=["rule-sets"])


@router.get("", response_model=list[RuleSetRead])
def get_rule_sets(session: SessionDep, _: AdminDep):
    return list_rule_sets(session)


@router.post("", response_model=RuleSetRead, status_code=201)
def post_rule_set(payload: RuleSetCreate, session: SessionDep, admin: AdminDep):
    try:
        return create_rule_set_version(session, payload.name, payload.config, admin)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="rule set version conflict") from exc
```

`router.py` include:

```python
from src.api.v1.routes_data_sources import router as data_sources_router
from src.api.v1.routes_rule_sets import router as rule_sets_router

api_router.include_router(data_sources_router)
api_router.include_router(rule_sets_router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd service && uv run --python 3.12 pytest tests/api/test_data_sources.py tests/api/test_rule_sets.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add service/src/api/v1 service/src/application/services service/tests/api
git commit -m "feat: 添加数据源与规则集 API"
```

---

### Task 3: 作业规划、文件任务、结果与系统状态 API

**Files:**
- Modify: `service/src/api/v1/schemas.py`
- Create: `service/src/application/services/jobs.py`
- Create: `service/src/application/services/queries.py`
- Create: `service/src/api/v1/routes_jobs.py`
- Create: `service/src/api/v1/routes_matches.py`
- Create: `service/src/api/v1/routes_system.py`
- Modify: `service/src/api/v1/router.py`
- Create: `service/tests/api/test_jobs.py`
- Create: `service/tests/api/test_matches.py`

**Interfaces:**
- Consumes: `SessionDep`, `AdminDep`, DTOs, data source/rule set services
- Produces: `JobFileFilter`, `JobCreate`, `JobRead`, `FileTaskRead`, `MatchRead`, `SystemStatusRead`
- Produces: `create_job(session, payload, created_by, idempotency_key) -> tuple[Job, bool]`
- Produces routes: `POST/GET /api/jobs`, `GET /api/jobs/{id}`, `GET /api/jobs/{id}/files`, `POST /api/jobs/{id}/cancel`, `GET /api/matches`, `GET /api/workers`, `GET /api/system/status`

- [ ] **Step 1: Write the failing tests**

`service/tests/api/test_jobs.py`:

```python
import zipfile


def make_nas(tmp_path):
    root = tmp_path / "nas"
    root.mkdir()
    batch = root / "batch"
    batch.mkdir()
    with zipfile.ZipFile(batch / "a.zip", "w") as archive:
        archive.writestr("app.log", "ERROR one\n")
    return root, batch


def seed_source_and_rules(client, auth_headers, root):
    source = client.post(
        "/api/data-sources",
        headers=auth_headers,
        json={"name": "nas", "root_path": str(root)},
    ).json()
    rules = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={
            "name": "baseline",
            "config": {
                "name": "baseline",
                "file_glob": "**/*.log",
                "rules": [
                    {"id": "e", "match": {"type": "keyword", "any": ["ERROR"]}}
                ],
            },
        },
    ).json()
    return source, rules


def test_create_job_creates_file_tasks(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    response = client.post(
        "/api/jobs",
        headers={**auth_headers, "Idempotency-Key": "job-1"},
        json={
            "name": "batch-1",
            "data_source_id": source["id"],
            "source_path": str(batch),
            "rule_set_id": rules["id"],
            "file_filter": {"recursive": True, "max_depth": 5},
        },
    )
    assert response.status_code == 201
    job = response.json()
    assert job["status"] == "running"
    assert job["total_files"] == 1

    files = client.get(f"/api/jobs/{job['id']}/files", headers=auth_headers)
    assert files.status_code == 200
    assert files.json()[0]["file_name"] == "a.zip"


def test_same_idempotency_key_returns_same_job(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    body = {
        "name": "batch-1",
        "data_source_id": source["id"],
        "source_path": str(batch),
        "rule_set_id": rules["id"],
        "file_filter": {"recursive": True, "max_depth": 5},
    }
    headers = {**auth_headers, "Idempotency-Key": "same-key"}
    first = client.post("/api/jobs", headers=headers, json=body)
    second = client.post("/api/jobs", headers=headers, json=body)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_source_path_outside_root_is_rejected(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    response = client.post(
        "/api/jobs",
        headers=auth_headers,
        json={
            "name": "bad",
            "data_source_id": source["id"],
            "source_path": str(tmp_path),
            "rule_set_id": rules["id"],
        },
    )
    assert response.status_code == 400


def test_empty_directory_succeeds(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    empty = root / "empty"
    empty.mkdir()
    source, rules = seed_source_and_rules(client, auth_headers, root)
    response = client.post(
        "/api/jobs",
        headers=auth_headers,
        json={
            "name": "empty",
            "data_source_id": source["id"],
            "source_path": str(empty),
            "rule_set_id": rules["id"],
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "succeeded"
    assert response.json()["total_files"] == 0
```

`service/tests/api/test_matches.py`:

```python
from src.domain.models.entities import Match


def test_query_matches_by_job(client, auth_headers, session) -> None:
    session.add(
        Match(
            job_id=7,
            file_task_id=9,
            rule_id="error",
            rule_set_id=1,
            rule_version=1,
            source_path="/nas/a.zip",
            member_path="app.log",
            file_name="app.log",
            line_no=3,
            raw_line="ERROR one",
            fields={"level": "ERROR"},
            severity="error",
        )
    )
    session.commit()

    response = client.get("/api/matches?job_id=7", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()[0]["line_no"] == 3
    assert response.json()[0]["raw_line"] == "ERROR one"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd service && uv run --python 3.12 pytest tests/api/test_jobs.py tests/api/test_matches.py -v`

Expected: FAIL because job/query routes do not exist.

- [ ] **Step 3: Write minimal implementation**

Add schemas:

```python
class JobFileFilter(BaseModel):
    recursive: bool = True
    max_depth: int = Field(default=5, ge=1, le=20)
    suffixes: set[str] | None = None
    max_files: int = Field(default=20_000, ge=1)


class JobCreate(BaseModel):
    name: str = Field(min_length=1)
    data_source_id: int
    source_path: str = Field(min_length=1)
    rule_set_id: int
    file_filter: JobFileFilter = Field(default_factory=JobFileFilter)


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    data_source_id: int
    source_path: str
    rule_set_id: int
    status: str
    total_files: int
    total_bytes: int
    cancel_requested: bool
    created_at: datetime
    error: str | None


class FileTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    job_id: int
    source_path: str
    file_name: str
    size_bytes: int
    status: str
    attempts: int
    processed_lines: int
    matched_rows: int
    error: str | None


class MatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    job_id: int
    rule_id: str
    rule_version: int
    source_path: str
    member_path: str
    file_name: str
    line_no: int
    raw_line: str
    fields: dict
    numeric_value: Decimal | None
    severity: str
    created_at: datetime


class SystemStatusRead(BaseModel):
    file_tasks: dict[str, int]
    pending_oldest_seconds: float | None
    workers_online: int
```

`service/src/application/services/jobs.py`:

```python
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from sqlmodel import Session, select

from src.api.v1.schemas import JobCreate
from src.domain.archive.reader import discover_archives
from src.domain.models.entities import DataSource, FileTask, Job, RuleSet


def _utc_from_timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


def create_job(
    session: Session,
    payload: JobCreate,
    created_by: str,
    idempotency_key: str | None,
) -> tuple[Job, bool]:
    if idempotency_key:
        existing = session.exec(
            select(Job).where(
                Job.created_by == created_by,
                Job.idempotency_key == idempotency_key,
            )
        ).first()
        if existing is not None:
            return existing, False

    source = session.get(DataSource, payload.data_source_id)
    if source is None or not source.enabled:
        raise HTTPException(status_code=404, detail="data source not found or disabled")
    rule_set = session.get(RuleSet, payload.rule_set_id)
    if rule_set is None:
        raise HTTPException(status_code=404, detail="rule set not found")

    root = Path(source.root_path).resolve()
    source_path = Path(payload.source_path).resolve()
    if not source_path.is_relative_to(root):
        raise HTTPException(status_code=400, detail="source_path is outside data source root")
    if not source_path.is_dir():
        raise HTTPException(status_code=400, detail="source_path is not a directory")

    filters = payload.file_filter
    files = discover_archives(
        source_path,
        recursive=filters.recursive,
        max_depth=filters.max_depth,
        suffixes=filters.suffixes,
    )
    if len(files) > filters.max_files:
        raise HTTPException(status_code=400, detail="too many files in source directory")

    job = Job(
        name=payload.name,
        data_source_id=source.id,
        source_path=str(source_path),
        file_filter=filters.model_dump(mode="json"),
        rule_set_id=rule_set.id,
        created_by=created_by,
        idempotency_key=idempotency_key,
        total_files=len(files),
        total_bytes=sum(path.stat().st_size for path in files),
        status="running" if files else "succeeded",
        started_at=datetime.now(UTC),
        finished_at=None if files else datetime.now(UTC),
    )
    session.add(job)
    session.flush()
    for path in files:
        stat = path.stat()
        session.add(
            FileTask(
                job_id=job.id,
                source_path=str(path),
                file_name=path.name,
                size_bytes=stat.st_size,
                mtime=_utc_from_timestamp(stat.st_mtime),
            )
        )
    session.commit()
    session.refresh(job)
    return job, True
```

Queries:

```python
from datetime import UTC, datetime

from sqlalchemy import func
from sqlmodel import Session, select

from src.domain.models.entities import FileTask, Job, Match, Worker


def list_jobs(session, limit=50, offset=0):
    return list(session.exec(select(Job).order_by(Job.created_at.desc(), Job.id.desc()).offset(offset).limit(limit)).all())


def list_file_tasks(session, job_id, limit=50, offset=0):
    return list(session.exec(select(FileTask).where(FileTask.job_id == job_id).order_by(FileTask.id).offset(offset).limit(limit)).all())


def list_matches(session, job_id=None, rule_id=None, file_name=None, cursor=None, limit=50):
    statement = select(Match)
    if job_id is not None:
        statement = statement.where(Match.job_id == job_id)
    if rule_id:
        statement = statement.where(Match.rule_id == rule_id)
    if file_name:
        statement = statement.where(Match.file_name == file_name)
    if cursor is not None:
        statement = statement.where(Match.id < cursor)
    return list(session.exec(statement.order_by(Match.id.desc()).limit(limit)).all())


def system_status(session):
    counts = dict(session.exec(select(FileTask.status, func.count()).group_by(FileTask.status)).all())
    oldest = session.exec(select(func.min(FileTask.scheduled_at)).where(FileTask.status == "pending")).one()
    pending_oldest = None
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        pending_oldest = max(0.0, (datetime.now(UTC) - oldest).total_seconds())
    online = session.exec(select(func.count()).select_from(Worker).where(Worker.status.in_(["online", "busy", "draining"]))).one()
    return {"file_tasks": counts, "pending_oldest_seconds": pending_oldest, "workers_online": online}
```

Routes:

- `POST /jobs` uses `Idempotency-Key` header; status 201/200; response `JobRead`.
- `GET /jobs`, `GET /jobs/{id}` (404), `GET /jobs/{id}/files`, `POST /jobs/{id}/cancel` (update pending tasks to cancelled, job cancel_requested true; if no running, cancelled), `GET /matches`, `GET /workers`, `GET /system/status`.
- Use `verify job` helper.
- Include routers.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd service && uv run --python 3.12 pytest tests/api -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add service/src/api/v1 service/src/application/services service/tests/api
git commit -m "feat: 添加作业规划与结果查询 API"
```

---

### Task 4: M1a 集成验证与文档

**Files:**
- Modify: `README.md`
- Modify: `service/README.md`
- Maybe Modify: `.env.example` (admin token)

- [ ] **Step 1: Run full tests and ruff**

```bash
cd service && uv run --python 3.12 pytest -q
cd service && uv run --python 3.12 ruff check src tests
```

Expected: all pass.

- [ ] **Step 2: Document M1a**

Update `service/README.md`:
- M1a endpoints: health, data sources, rule sets, jobs, files, matches, workers, system status.
- Auth: `Authorization: Bearer $LOGHARVEST_ADMIN_TOKEN`.
- Create job flow: data source root → source path → scan compressed files → `file_tasks`.
- M1b not included: PostgreSQL `SKIP LOCKED` claim loop, worker runtime, SSE, frontend, Alembic.

Update `.env.example`:
```dotenv
LOGHARVEST_ADMIN_TOKEN=dev-admin-token
```

- [ ] **Step 3: Commit**

```bash
git add README.md service/README.md .env.example
git commit -m "docs: 补充 M1a 控制面 API 说明"
```

---

## Plan Self-Review

- API foundation/auth/session fixtures are covered.
- Data source and rule set version APIs are covered.
- Job planning scans NAS recursively, creates one `file_task` per compressed file, rejects path escape, handles idempotency and empty directories.
- Results/system/worker read APIs are covered.
- M1b explicitly excludes PG queue, worker loop, SSE, frontend, Alembic.
- All tests use SQLite and dependency overrides; no PostgreSQL required.
