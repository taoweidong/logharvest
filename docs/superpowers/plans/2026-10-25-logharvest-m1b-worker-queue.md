# LogHarvest M1b Worker/Queue 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付第一阶段剩余后端：PostgreSQL `SKIP LOCKED` 任务队列适配、Worker 常驻领取循环、SSE 进度、Worker drain/resume 管理。

**Architecture:** `PostgresTaskRepository` 用原生 SQL 原子领取 `pending` 任务并写入 `file_task_attempts`；`PostgresMatchWriter` 批量写 `matches`；`WorkerRuntime` 复用 M0 `TaskProcessor`。控制面新增 SSE 作业进度和 Worker 管理接口。M1b 不实现前端、Alembic、PG 真实集成测试环境。

**Tech Stack:** Python 3.12、SQLAlchemy/SQLModel、psycopg 3、FastAPI、SQLite（API 测试）、`uv`。

**Spec:**
- [主设计方案](../specs/2026-10-24-compressed-log-extraction-design.md)
- [M1a 计划](./2026-10-25-logharvest-m1a-control-plane-api.md)

## Global Constraints

- 任务队列使用 PostgreSQL `file_tasks` + `FOR UPDATE SKIP LOCKED`；不引入 Celery/Redis。
- Worker 不绑定作业；完成一个任务后继续领取；无任务时轮询等待。
- Worker 在领取前检查 `workers.drain_requested`；drain 后只完成当前任务。
- 原始 NAS 只读；Worker 只读源文件，复制到本地临时目录。
- 重试幂等：领取时 attempts+1，写入 attempt 历史；失败按 max_attempts 退避；重试前清理旧 matches。
- M1b 不实现：前端、Alembic、SSE 前端页面、真实 PostgreSQL 集成环境。
- 所有新代码需要测试；无 PostgreSQL 时允许 SQL 编译/协议 fake 测试 + 显式跳过 integration。
- 命令从 `service/` 执行：`uv run --python 3.12 ...`；中文 `feat/fix/docs` 提交。

---

### Task 1: PostgreSQL 队列、结果写入与 Worker 注册

**Files:**
- Modify: `service/src/application/worker/ports.py` (add `rule_config` to `ClaimedTask`)
- Create: `service/src/infrastructure/task_queue.py`
- Create: `service/src/infrastructure/match_writer.py`
- Create: `service/tests/infrastructure/test_task_queue.py`
- Create: `service/tests/infrastructure/test_match_writer.py`

**Interfaces:**
- Produces: `CLAIM_SQL`, `PostgresTaskRepository(engine, max_attempts=5)`
- Methods: `claim_next(worker_id, worker_version) -> ClaimedTask | None`, `heartbeat(task_id, attempts, processed_lines, matched_rows) -> bool`, `complete(task_id, attempts, status, processed_lines, matched_rows, error=None) -> None`, `recover_stale(stale_seconds=120) -> int`, `is_draining(worker_id) -> bool`, `register_worker(worker_id, hostname, version)`, `heartbeat_worker(worker_id, status, current_task_id=None)`
- Produces: `PostgresMatchWriter(engine)`, `delete_for_task`, `write_batch`.
- `ClaimedTask` adds `rule_config: dict[str, Any] = field(default_factory=dict)`

- [ ] **Step 1: Write the failing tests**

`service/tests/infrastructure/test_task_queue.py`:

```python
from src.infrastructure.task_queue import CLAIM_SQL, COMPLETE_SQL, PostgresTaskRepository
from sqlalchemy import create_engine


def test_claim_sql_uses_skip_locked() -> None:
    normalized = " ".join(CLAIM_SQL.split()).upper()
    assert "FOR UPDATE" in normalized
    assert "SKIP LOCKED" in normalized
    assert "STATUS = 'PENDING'" in normalized


def test_repository_can_be_constructed_without_connecting() -> None:
    engine = create_engine("sqlite://")
    repo = PostgresTaskRepository(engine)
    assert repo is not None


def test_complete_sql_marks_finished_and_attempt_status() -> None:
    normalized = " ".join(COMPLETE_SQL.split()).upper()
    assert "FINISHED_AT" in normalized
    assert "FILE_TASK_ATTEMPTS" in normalized
```

`service/tests/infrastructure/test_match_writer.py`:

```python
from sqlalchemy import create_engine
from src.infrastructure.match_writer import PostgresMatchWriter


def test_writer_can_be_constructed_without_connecting() -> None:
    writer = PostgresMatchWriter(create_engine("sqlite://"))
    assert writer is not None


def test_delete_sql_targets_file_task() -> None:
    from src.infrastructure.match_writer import DELETE_BY_TASK_SQL

    assert "file_task_id" in DELETE_BY_TASK_SQL
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd service && uv run --python 3.12 pytest tests/infrastructure -v`

Expected: FAIL because modules do not exist.

- [ ] **Step 3: Write minimal implementation**

`service/src/application/worker/ports.py` add to `ClaimedTask`:

```python
from dataclasses import dataclass, field
from typing import Any

# inside ClaimedTask after fallback_encoding
rule_config: dict[str, Any] = field(default_factory=dict)
```

`service/src/infrastructure/task_queue.py`:

```python
from dataclasses import replace
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.application.worker.ports import ClaimedTask

CLAIM_SQL = """
WITH picked AS (
    SELECT t.id
    FROM file_tasks t
    JOIN jobs j ON j.id = t.job_id
    WHERE t.status = 'pending'
      AND t.scheduled_at <= now()
      AND j.cancel_requested = false
    ORDER BY t.scheduled_at, t.id
    FOR UPDATE OF t SKIP LOCKED
    LIMIT 1
)
UPDATE file_tasks t
SET status = 'running',
    worker_id = :worker_id,
    attempts = t.attempts + 1,
    started_at = COALESCE(t.started_at, now()),
    heartbeat_at = now()
FROM picked p
WHERE t.id = p.id
RETURNING t.id, t.job_id, t.source_path, t.file_name, t.attempts;
"""

COMPLETE_SQL = """
WITH target AS (
    SELECT id, attempts, max_attempts
    FROM file_tasks
    WHERE id = :task_id AND attempts = :attempts
)
UPDATE file_tasks t
SET status = CASE
        WHEN :status = 'failed' AND target.attempts < target.max_attempts THEN 'pending'
        ELSE :status
    END,
    scheduled_at = CASE
        WHEN :status = 'failed' AND target.attempts < target.max_attempts
        THEN now() + make_interval(secs => target.attempts * 5)
        ELSE t.scheduled_at
    END,
    processed_lines = :processed_lines,
    matched_rows = :matched_rows,
    finished_at = CASE WHEN :status = 'failed' AND target.attempts < target.max_attempts
                       THEN NULL ELSE now() END,
    error = :error
FROM target
WHERE t.id = target.id
RETURNING t.status;
"""


class PostgresTaskRepository:
    def __init__(self, engine: Engine, max_attempts: int = 5) -> None:
        self._engine = engine
        self._max_attempts = max_attempts

    def register_worker(self, worker_id: str, hostname: str, version: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO workers (id, hostname, version, status, started_at, last_heartbeat)
                    VALUES (:id, :hostname, :version, 'online', now(), now())
                    ON CONFLICT (id) DO UPDATE
                    SET hostname = EXCLUDED.hostname,
                        version = EXCLUDED.version,
                        status = 'online',
                        last_heartbeat = now()
                    """
                ),
                {"id": worker_id, "hostname": hostname, "version": version},
            )

    def heartbeat_worker(self, worker_id: str, status: str, current_task_id: int | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE workers SET status=:status, current_task_id=:task_id, "
                    "last_heartbeat=now() WHERE id=:id"
                ),
                {"id": worker_id, "status": status, "task_id": current_task_id},
            )

    def is_draining(self, worker_id: str) -> bool:
        with self._engine.connect() as conn:
            value = conn.execute(
                text("SELECT drain_requested FROM workers WHERE id=:id"),
                {"id": worker_id},
            ).scalar_one_or_none()
        return bool(value)

    def claim_next(self, worker_id: str, worker_version: str) -> ClaimedTask | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text(CLAIM_SQL),
                {"worker_id": worker_id},
            ).mappings().first()
            if row is None:
                return None
            rule = conn.execute(
                text(
                    """
                    SELECT j.rule_set_id, r.version, r.config
                    FROM jobs j JOIN rule_sets r ON r.id = j.rule_set_id
                    WHERE j.id = :job_id
                    """
                ),
                {"job_id": row["job_id"]},
            ).mappings().one()
            conn.execute(
                text(
                    """
                    INSERT INTO file_task_attempts
                        (file_task_id, attempt_no, worker_id, worker_version, status, started_at)
                    VALUES (:task_id, :attempt_no, :worker_id, :worker_version, 'running', now())
                    """
                ),
                {
                    "task_id": row["id"],
                    "attempt_no": row["attempts"],
                    "worker_id": worker_id,
                    "worker_version": worker_version,
                },
            )
            return ClaimedTask(
                id=row["id"],
                job_id=row["job_id"],
                source_path=__import__("pathlib").Path(row["source_path"]),
                file_name=row["file_name"],
                attempts=row["attempts"],
                rule_set_id=rule["rule_set_id"],
                rule_version=rule["version"],
                rule_config=rule["config"],
            )

    def heartbeat(self, task_id: int, attempts: int, processed_lines: int, matched_rows: int) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE file_tasks
                    SET heartbeat_at=now(), processed_lines=:processed_lines,
                        matched_rows=:matched_rows
                    WHERE id=:id AND attempts=:attempts AND status='running'
                    """
                ),
                {
                    "id": task_id,
                    "attempts": attempts,
                    "processed_lines": processed_lines,
                    "matched_rows": matched_rows,
                },
            )
            return result.rowcount == 1

    def complete(self, task_id, attempts, status, processed_lines, matched_rows, error=None) -> None:
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
                },
            )
            conn.execute(
                text(
                    "UPDATE file_task_attempts SET status=:status, finished_at=now(), "
                    "processed_lines=:processed_lines, matched_rows=:matched_rows, error=:error "
                    "WHERE file_task_id=:task_id AND attempt_no=:attempts"
                ),
                {
                    "task_id": task_id,
                    "attempts": attempts,
                    "status": status,
                    "processed_lines": processed_lines,
                    "matched_rows": matched_rows,
                    "error": error,
                },
            )

    def recover_stale(self, stale_seconds: int = 120) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    WITH stale AS (
                        SELECT id, attempts, max_attempts
                        FROM file_tasks
                        WHERE status='running'
                          AND (heartbeat_at IS NULL OR heartbeat_at < now() - make_interval(secs => :seconds))
                    ), updated AS (
                        UPDATE file_tasks t
                        SET status = CASE WHEN stale.attempts < stale.max_attempts THEN 'pending' ELSE 'failed' END,
                            scheduled_at = now(),
                            error = COALESCE(t.error, 'worker heartbeat timeout')
                        FROM stale
                        WHERE t.id = stale.id
                        RETURNING t.id
                    )
                    UPDATE file_task_attempts a
                    SET status='timeout', finished_at=now()
                    WHERE a.file_task_id IN (SELECT id FROM updated)
                      AND a.status='running'
                    """
                ),
                {"seconds": stale_seconds},
            )
            return result.rowcount
```

`service/src/infrastructure/match_writer.py`:

```python
from collections.abc import Sequence

from sqlalchemy import delete, insert
from sqlalchemy.engine import Engine

from src.application.worker.ports import MatchRecord
from src.domain.models.entities import Match

DELETE_BY_TASK_SQL = "DELETE FROM matches WHERE file_task_id = :task_id"


class PostgresMatchWriter:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def delete_for_task(self, task_id: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(delete(Match).where(Match.file_task_id == task_id))

    def write_batch(self, matches: Sequence[MatchRecord]) -> None:
        if not matches:
            return
        with self._engine.begin() as conn:
            conn.execute(
                insert(Match),
                [
                    {
                        "job_id": m.job_id,
                        "file_task_id": m.file_task_id,
                        "rule_id": m.rule_id,
                        "rule_set_id": m.rule_set_id,
                        "rule_version": m.rule_version,
                        "severity": m.severity,
                        "tags": m.tags,
                        "source_path": m.source_path,
                        "member_path": m.member_path,
                        "file_name": m.file_name,
                        "line_no": m.line_no,
                        "raw_line": m.raw_line,
                        "raw_truncated": m.raw_truncated,
                        "fields": m.fields,
                        "numeric_value": m.numeric_value,
                    }
                    for m in matches
                ],
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd service && uv run --python 3.12 pytest tests/infrastructure -v`

Expected: PASS (SQL/constructor tests; no PG connection).

- [ ] **Step 5: Commit**

```bash
git add service/src/application/worker/ports.py service/src/infrastructure service/tests/infrastructure
git commit -m "feat: 添加 PostgreSQL 队列与结果写入适配器"
```

---

### Task 2: Worker 常驻循环、SSE 与 Worker 管理

**Files:**
- Create: `service/src/worker/__init__.py`
- Create: `service/src/worker/main.py`
- Create: `service/src/worker/__main__.py`
- Modify: `service/src/api/v1/router.py`
- Create: `service/src/api/v1/routes_workers.py`
- Create: `service/src/api/v1/routes_events.py`
- Create: `service/tests/worker/test_runtime.py`
- Create: `service/tests/api/test_workers.py`
- Create: `service/tests/api/test_events.py`

**Interfaces:**
- Consumes: `PostgresTaskRepository`, `PostgresMatchWriter`, `TaskProcessor`, `compile_ruleset`, `RuleSetConfig`
- Produces: `WorkerRuntime.run_once() -> bool`, `WorkerRuntime.run_forever(stop_event)`
- Produces routes: `POST /api/workers/{id}/drain`, `POST /api/workers/{id}/resume`, `POST /api/workers/drain-version`, `GET /api/jobs/{id}/events` (SSE)

- [ ] **Step 1: Write failing tests**

`service/tests/worker/test_runtime.py`:

```python
import zipfile
from pathlib import Path

from src.application.worker.ports import ClaimedTask
from src.domain.rules.evaluator import compile_ruleset
from src.domain.rules.models import RuleSetConfig
from src.worker.main import WorkerRuntime


class FakeRepo:
    def __init__(self, task, draining=False):
        self.task = task
        self.draining = draining
        self.completed = []

    def is_draining(self, worker_id):
        return self.draining

    def claim_next(self, worker_id, worker_version):
        task, self.task = self.task, None
        return task

    def heartbeat_worker(self, worker_id, status, current_task_id=None):
        pass

    def heartbeat(self, *args, **kwargs):
        return True

    def complete(self, *args, **kwargs):
        self.completed.append((args, kwargs))


class FakeWriter:
    def __init__(self):
        self.written = []

    def delete_for_task(self, task_id):
        pass

    def write_batch(self, matches):
        self.written.extend(matches)


def make_task(tmp_path: Path):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("app.log", "ERROR one\n")
    return ClaimedTask(
        id=1,
        job_id=2,
        source_path=archive,
        file_name="a.zip",
        attempts=1,
        rule_set_id=3,
        rule_version=1,
        rule_config={
            "name": "t",
            "file_glob": "**/*.log",
            "rules": [
                {"id": "e", "match": {"type": "keyword", "any": ["ERROR"]}}
            ],
        },
    )


def test_run_once_processes_claimed_task(tmp_path):
    repo = FakeRepo(make_task(tmp_path))
    writer = FakeWriter()
    runtime = WorkerRuntime(
        repo=repo,
        writer=writer,
        worker_id="w1",
        worker_version="0.1.0",
        work_dir=tmp_path / "work",
        rules_factory=lambda task: compile_ruleset(
            RuleSetConfig.model_validate(task.rule_config)
        ),
    )
    assert runtime.run_once() is True
    assert len(writer.written) == 1
    assert repo.completed
```

`__main__.py`:

```python
from src.worker.main import main

if __name__ == "__main__":
    main()
```

`service/tests/api/test_workers.py`:

```python
from src.domain.models.entities import Worker


def test_drain_and_resume_worker(client, auth_headers, session):
    session.add(Worker(id="w1", hostname="h1", version="0.1.0", status="online"))
    session.commit()
    drain = client.post("/api/workers/w1/drain", headers=auth_headers)
    assert drain.status_code == 200
    assert drain.json()["drain_requested"] is True
    resume = client.post("/api/workers/w1/resume", headers=auth_headers)
    assert resume.status_code == 200
    assert resume.json()["drain_requested"] is False


def test_drain_by_version(client, auth_headers, session):
    session.add(Worker(id="w1", hostname="h1", version="0.1.0", status="online"))
    session.add(Worker(id="w2", hostname="h2", version="0.2.0", status="online"))
    session.commit()
    response = client.post("/api/workers/drain-version?version=0.1.0", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["drained"] == 1
```

`service/tests/api/test_events.py`:

```python
def test_job_events_sse_returns_snapshot(client, auth_headers, session):
    from src.domain.models.entities import Job

    session.add(Job(name="j", data_source_id=1, source_path="/nas", rule_set_id=1, status="running"))
    session.commit()
    response = client.get("/api/jobs/1/events?max_events=1", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data:" in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd service && uv run --python 3.12 pytest tests/worker tests/api/test_workers.py tests/api/test_events.py -v`

Expected: FAIL because worker/routes do not exist.

- [ ] **Step 3: Write minimal implementation**

`service/src/worker/main.py`:

```python
import asyncio
import os
import signal
import socket
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any

from src.application.worker.ports import ClaimedTask
from src.application.worker.processor import TaskProcessor
from src.domain.rules.evaluator import CompiledRuleSet, compile_ruleset
from src.domain.rules.models import RuleSetConfig
from src.infrastructure.database import engine
from src.infrastructure.match_writer import PostgresMatchWriter
from src.infrastructure.task_queue import PostgresTaskRepository


class WorkerRuntime:
    def __init__(
        self,
        repo,
        writer,
        worker_id: str,
        worker_version: str,
        work_dir: Path,
        rules_factory: Callable[[ClaimedTask], CompiledRuleSet] = lambda task: compile_ruleset(
            RuleSetConfig.model_validate(task.rule_config)
        ),
        poll_seconds: float = 1.0,
    ) -> None:
        self._repo = repo
        self._writer = writer
        self._worker_id = worker_id
        self._worker_version = worker_version
        self._work_dir = work_dir
        self._rules_factory = rules_factory
        self._poll_seconds = poll_seconds

    def run_once(self) -> bool:
        if self._repo.is_draining(self._worker_id):
            return False
        task = self._repo.claim_next(self._worker_id, self._worker_version)
        if task is None:
            return False
        self._repo.heartbeat_worker(self._worker_id, "busy", task.id)
        processor = TaskProcessor(
            repo=self._repo,
            writer=self._writer,
            rules=self._rules_factory(task),
            work_dir=self._work_dir,
        )
        processor.process(task)
        self._repo.heartbeat_worker(self._worker_id, "online")
        return True

    def run_forever(self, stop_event: Event | None = None) -> None:
        stop = stop_event or Event()
        hostname = socket.gethostname()
        self._repo.register_worker(self._worker_id, hostname, self._worker_version)
        try:
            while not stop.is_set():
                worked = self.run_once()
                if not worked:
                    stop.wait(self._poll_seconds)
                self._repo.heartbeat_worker(self._worker_id, "online")
        finally:
            self._repo.heartbeat_worker(self._worker_id, "offline")


def main() -> None:
    worker_id = os.getenv("LOGHARVEST_WORKER_ID", socket.gethostname())
    version = os.getenv("LOGHARVEST_WORKER_VERSION", "0.1.0")
    work_dir = Path(os.getenv("LOGHARVEST_WORK_DIR", tempfile.gettempdir())) / "logharvest" / worker_id
    runtime = WorkerRuntime(
        repo=PostgresTaskRepository(engine),
        writer=PostgresMatchWriter(engine),
        worker_id=worker_id,
        worker_version=version,
        work_dir=work_dir,
    )
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    runtime.run_forever(stop)


if __name__ == "__main__":
    main()
```

`routes_workers.py`:

```python
from fastapi import APIRouter, HTTPException
from sqlmodel import select

from src.api.dependencies import AdminDep, SessionDep
from src.api.v1.schemas import WorkerRead
from src.domain.models.entities import Worker

router = APIRouter(prefix="/workers", tags=["workers"])


@router.post("/{worker_id}/drain")
def drain_worker(worker_id: str, session: SessionDep, _: AdminDep):
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker not found")
    worker.drain_requested = True
    worker.status = "draining"
    session.add(worker)
    session.commit()
    return {"id": worker.id, "drain_requested": worker.drain_requested}


@router.post("/{worker_id}/resume")
def resume_worker(worker_id: str, session: SessionDep, _: AdminDep):
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker not found")
    if worker.status == "offline":
        raise HTTPException(status_code=409, detail="worker is offline")
    worker.drain_requested = False
    worker.status = "online"
    session.add(worker)
    session.commit()
    return {"id": worker.id, "drain_requested": worker.drain_requested}


@router.post("/drain-version")
def drain_version(version: str, session: SessionDep, _: AdminDep):
    workers = list(session.exec(select(Worker).where(Worker.version == version)).all())
    for worker in workers:
        worker.drain_requested = True
        worker.status = "draining"
        session.add(worker)
    session.commit()
    return {"drained": len(workers)}
```

`routes_events.py`:

```python
import asyncio
import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from src.api.dependencies import AdminDep, SessionDep
from src.domain.models.entities import FileTask, Job

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _snapshot(session: Session, job_id: int) -> dict:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    tasks = list(session.exec(select(FileTask).where(FileTask.job_id == job_id)).all())
    statuses = {}
    for task in tasks:
        statuses[task.status] = statuses.get(task.status, 0) + 1
    return {
        "job_id": job_id,
        "status": job.status,
        "total": len(tasks),
        "by_status": statuses,
        "processed_lines": sum(task.processed_lines for task in tasks),
        "matched_rows": sum(task.matched_rows for task in tasks),
    }


@router.get("/{job_id}/events")
async def job_events(
    job_id: int,
    session: SessionDep,
    _: AdminDep,
    max_events: int = Query(default=0, ge=0, le=100),
):
    _snapshot(session, job_id)  # validate existence before streaming

    async def stream():
        sent = 0
        while max_events == 0 or sent < max_events:
            with Session(session.get_bind()) as snapshot_session:
                payload = _snapshot(snapshot_session, job_id)
            yield f"event: progress\ndata: {json.dumps(payload, default=str)}\n\n"
            sent += 1
            if max_events and sent >= max_events:
                break
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")
```

`router.py` include `workers_router` and `events_router`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd service && uv run --python 3.12 pytest tests/worker tests/api/test_workers.py tests/api/test_events.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add service/src/worker service/src/api/v1 service/tests/worker service/tests/api
git commit -m "feat: 添加 Worker 循环、SSE 与 Worker 管理"
```

---

### Task 3: M1b 集成验证与文档

**Files:**
- Modify: `README.md`
- Modify: `service/README.md`
- Modify: `.env.example`

- [ ] **Step 1: Run full tests and ruff**

```bash
cd service && uv run --python 3.12 pytest -q
cd service && uv run --python 3.12 ruff check src tests
```

Expected: all pass.

- [ ] **Step 2: Document worker runtime and API**

Update `service/README.md`:
- Worker command: `uv run --python 3.12 python -m src.worker`
- Env: `LOGHARVEST_WORKER_ID`, `LOGHARVEST_WORKER_VERSION`, `LOGHARVEST_WORK_DIR`
- Worker drain/resume/drain-version and SSE endpoint.
- PostgreSQL queue SQL is implemented but requires a real PostgreSQL instance; M1b tests cover SQL/constructors and use fake runtime; no local PG integration.
- M1b still excludes frontend and Alembic.

Update `.env.example` with worker vars.

- [ ] **Step 3: Commit**

```bash
git add README.md service/README.md .env.example
git commit -m "docs: 补充 M1b Worker 与 SSE 说明"
```

---

## Plan Self-Review

- Queue adapter, match writer, attempts/heartbeat/retry/stale recovery are covered.
- Worker runtime, registration, drain/resume, and SSE are covered.
- No frontend/Alembic/real PG integration included; explicitly documented.
- Tests use SQL/fake runtime and SQLite API fixtures; PostgreSQL behavior requires an external PG instance in a later integration task.
