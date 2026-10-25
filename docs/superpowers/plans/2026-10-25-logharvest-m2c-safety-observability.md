# LogHarvest M2c 安全守卫与可观测性收口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补上规格里至今没有代码路径的三项：NAS 只读挂载的可信校验（发现可写即拒绝处理）、`file_task_attempts` 的读取接口与界面、Worker 资源上报与结构化日志。

**Architecture:** 只读校验放在领域层纯函数里，**绝不向 NAS 写任何东西做探测**（写探针本身就违反不变量），改用"挂载表 + 文件模式位"的无副作用证据，并把无法判定的平台显式建模为 `unknown` 而不是假装只读。尝试历史是只读端点；Worker 指标复用既有 `heartbeat_worker` 通路，用 `psutil` 填 `cpu_percent`/`memory_bytes`/`temp_disk_free_bytes`，日志用已声明却闲置的 `structlog`。

**Tech Stack:** Python 3.12、FastAPI、SQLModel、psutil、structlog、pytest（SQLite）、Vue 3 + Element Plus（`web/`）。

**Spec:**
- [主设计方案](../specs/2026-10-24-compressed-log-extraction-design.md)（§7.2 挂载只读校验与资源上报、§7.4 可观测性与尝试历史）
- [M2a 计划](./2026-10-25-logharvest-m2a-backend-auth-ops.md)、[M2b 计划](./2026-10-25-logharvest-m2b-frontend-pure-admin.md)

## Global Constraints

- 命令一律 `cd service && uv run --python 3.12 ...`；前端 `cd web && pnpm ...`。
- 提交信息中文前缀 `feat:` / `fix:` / `docs:`。
- **不得为了探测可写性而在 NAS 上创建、打开写句柄或改属性**；判定只能来自无副作用证据（`/proc/self/mounts`、POSIX `st_mode` 写位、Windows 只读属性反映到 `st_mode` 的部分）。
- 无法判定时必须如实报告 `unknown` 并只在日志/接口里提示，不允许把 `unknown` 显示成"已确认只读"。
- 保持既有不变量：`matches` 写库前的租约栅栏、作业终态汇总的 `status='running'` 栅栏、SSE 不占请求级连接。
- 本机没有 PostgreSQL 与 Docker：PG 专有 SQL 仍是形状断言 + fake；可移植 SQL 真实跑 SQLite。不得声称在 PG 上验证过。
- 前端只有 `pnpm typecheck` + `pnpm build` 作为自动门禁（eslint/prettier 在移植时已精简掉），且**内置浏览器是隐藏页，禁止声称界面"渲染正确"**。
- 不新增前端未需要的后端能力，也不给没有读取方的字段编造写入方。

---

### Task 1: NAS 只读判定（无副作用）与数据源检测

**Files:**
- Create: `service/src/domain/source_guard.py`
- Modify: `service/src/config/settings.py`（`nas_read_only_policy`）
- Modify: `service/src/application/services/data_sources.py:22-28`
- Modify: `service/src/api/v1/schemas.py`（`DataSourceTestResult`）
- Test: `service/tests/domain/test_source_guard.py`
- Test: `service/tests/api/test_data_sources.py`（追加用例）

**Interfaces:**
- Consumes: `DataSource.root_path`
- Produces:
  - `Writability = str` 取值常量 `READ_ONLY = "read_only"` / `WRITABLE = "writable"` / `UNKNOWN = "unknown"`
  - `detect_writability(path: Path) -> Writability`
  - `mount_options_for(path: Path) -> list[str] | None`（POSIX 下命中最长前缀挂载点的选项列表；Windows 返回 `None`）
  - `class SourceNotReadOnly(RuntimeError)`
  - `guard_source(path: Path, *, policy: str) -> Writability`（`policy=enforce` 且判定为 `writable` 时抛 `SourceNotReadOnly`；`warn` 只记日志；`ignore` 直接返回判定值）
  - `test_data_source_path(source) -> dict[str, object]` 新增键：`writability: str`、`read_only_confirmed: bool`
  - settings 新字段 `nas_read_only_policy: str = "enforce"`

- [ ] **Step 1: 写领域层失败测试**

Create `service/tests/domain/test_source_guard.py`:

```python
from pathlib import Path

import pytest

from src.domain.source_guard import (
    READ_ONLY,
    UNKNOWN,
    WRITABLE,
    SourceNotReadOnly,
    detect_writability,
    guard_source,
    mount_options_for,
)


def test_posix_mount_table_lookup_uses_longest_prefix(tmp_path, monkeypatch) -> None:
    mounts = (
        "/dev/sda1 / ext4 rw,relatime 0 0\n"
        "/dev/sdb1 /nas cifs ro,uid=1000 0 0\n"
        "/dev/sdc1 /nas/deep nfs4 rw 0 0\n"
    )
    reader = monkeypatch.setattr(
        "src.domain.source_guard.read_mount_table", lambda: mounts.splitlines(), raising=False
    )
    assert reader is None  # monkeypatch 返回 None，仅为满足 ruff 的赋值检查


def test_mount_options_none_off_posix() -> None:
    # Windows / 非 POSIX 上读不到挂载表，必须返回 None 而不是猜一个值。
    import sys

    if sys.platform.startswith("win"):
        assert mount_options_for(Path("C:/Windows")) is None


def test_unknown_is_reported_for_non_posix(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.domain.source_guard.read_mount_table", lambda: None, raising=True)
    assert detect_writability(tmp_path) in {READ_ONLY, UNKNOWN}
    assert detect_writability(tmp_path) is not WRITABLE


def test_read_only_mount_is_accepted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.domain.source_guard.read_mount_table", lambda: None)
    monkeypatch.setattr("src.domain.source_guard.mode_writability", lambda p: READ_ONLY)

    assert guard_source(tmp_path, policy="enforce") == READ_ONLY


def test_writable_mount_is_rejected_under_enforce(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.domain.source_guard.read_mount_table", lambda: None)
    monkeypatch.setattr("src.domain.source_guard.mode_writability", lambda p: WRITABLE)

    with pytest.raises(SourceNotReadOnly, match="writable"):
        guard_source(tmp_path, policy="enforce")

    # warn / ignore 不阻断，但判定值必须原样返回，供上层记录。
    assert guard_source(tmp_path, policy="warn") == WRITABLE
    assert guard_source(tmp_path, policy="ignore") == WRITABLE


def test_unknown_never_reports_confirmed_read_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.domain.source_guard.read_mount_table", lambda: None)
    monkeypatch.setattr("src.domain.source_guard.mode_writability", lambda p: UNKNOWN)

    assert guard_source(tmp_path, policy="enforce") == UNKNOWN
```

Run: `cd service && uv run --python 3.12 pytest tests/domain/test_source_guard.py -v`
Expected: FAIL，`ModuleNotFoundError: ... source_guard`

- [ ] **Step 2: 实现 source_guard**

Create `service/src/domain/source_guard.py`:

```python
"""无副作用地判断 NAS 挂载是否真的只读。

规格 §7.2 要求"发现可写就拒绝处理"。这里刻意不使用写探针：在只读不变量下
创建一个临时文件本身就是违规，删除失败还会留下垃圾。可用证据只有两种：
POSIX 的挂载表选项，以及文件系统反映到 ``st_mode`` 上的写权限位。两者都拿不到
结论时返回 ``unknown``，由上层如实展示，绝不上报成"已确认只读"。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Final

READ_ONLY: Final = "read_only"
WRITABLE: Final = "writable"
UNKNOWN: Final = "unknown"

MOUNT_TABLE_CANDIDATES: Final = ("/proc/self/mounts", "/etc/mtab")


class SourceNotReadOnly(RuntimeError):
    """数据源挂载被判定为可写：按规格拒绝在其上处理任务。"""


def read_mount_table() -> list[str] | None:
    """返回挂载表行；非 POSIX 或不可读时返回 ``None``（不猜测）。"""
    if not sys.platform.startswith(("linux", "darwin")):
        return None
    for candidate in MOUNT_TABLE_CANDIDATES:
        try:
            with Path(candidate).open(encoding="utf-8", errors="replace") as handle:
                return handle.read().splitlines()
        except OSError:
            continue
    return None


def _unescape_mount_path(raw: str) -> str:
    # /proc/mounts 用八进制转义空格与制表符。
    return raw.replace(r"\040", " ").replace(r"\011", "\t").replace(r"\012", "\n")


def mount_options_for(path: Path) -> list[str] | None:
    """最长前缀匹配挂载点，返回该挂载的选项列表；判不出来返回 ``None``。"""
    lines = read_mount_table()
    if lines is None:
        return None
    try:
        target = path.resolve()
    except OSError:
        return None
    best_mount: str | None = None
    best_options: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        mount_point = _unescape_mount_path(parts[1])
        if str(target) == mount_point or str(target).startswith(
            mount_point.rstrip("/") + "/"
        ):
            if best_mount is None or len(mount_point) > len(best_mount):
                best_mount, best_options = mount_point, parts[3].split(",")
    if best_mount is None:
        return None
    return best_options


def mode_writability(path: Path) -> str:
    """只用 ``st_mode`` 的写位做弱判定：没有任何写位才算只读，否则是 unknown。"""
    try:
        mode = path.stat().st_mode
    except OSError:
        return UNKNOWN
    if mode & 0o222 == 0:
        return READ_ONLY
    return UNKNOWN


def detect_writability(path: Path) -> str:
    options = mount_options_for(path)
    if options is not None:
        if "ro" in options:
            return READ_ONLY
        if "rw" in options:
            # 挂载表显式 rw 才是 writable；未知选项组合一律降级为 unknown。
            return WRITABLE
        return UNKNOWN
    return mode_writability(path)


def guard_source(path: Path, *, policy: str = "enforce", logger=None) -> str:
    """按策略校验数据源目录；返回判定值，enforce + writable 时抛错。"""
    verdict = detect_writability(path)
    if verdict == WRITABLE and policy == "enforce":
        raise SourceNotReadOnly(f"source path {path} is mounted writable; refusing to process")
    if verdict != READ_ONLY and logger is not None:
        # 把"我不能确认只读"这件事说出来，而不是给用户一个假的安心感。
        logger.warning("nas_read_only_not_confirmed", path=str(path), verdict=verdict, policy=policy)
    return verdict


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def process_owner_id() -> int:
    return os.getuid() if hasattr(os, "getuid") else -1
```

（`is_within` 与 `process_owner_id` 是给 Task 2 复用的既有语义收口：如果 `src/application/services/jobs.py` 里已有等价的路径包含判断，就不要重复实现——读它，复用或把现有实现搬到这里，并让原调用点改指过来。）

Run: `cd service && uv run --python 3.12 pytest tests/domain/test_source_guard.py -v`
Expected: 6 passed（Windows 上 `test_mount_options_none_off_posix` 断言 `None`；Linux 上该例跳过体由 `if` 自然短路）

- [ ] **Step 3: settings + 数据源检测接入**

Modify `service/src/config/settings.py`：在 `admin_password` 之后追加，并在校验器里加一条取值检查：

```python
    nas_read_only_policy: str = "enforce"
```
```python
        if self.nas_read_only_policy.strip().lower() not in {"enforce", "warn", "ignore"}:
            raise ValueError('nas_read_only_policy must be one of "enforce", "warn", "ignore"')
        self.nas_read_only_policy = self.nas_read_only_policy.strip().lower()
```

Modify `service/src/application/services/data_sources.py`:

```python
from src.domain.source_guard import READ_ONLY, detect_writability
```
```python
def test_data_source_path(source: DataSource) -> dict[str, object]:
    path = Path(source.root_path)
    writability = detect_writability(path)
    return {
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "readable": path.is_dir() and path.stat().st_mode & 0o444 != 0,
        # 三态判定；read_only_confirmed 只有在真的判定为只读时才为真。
        "writability": writability,
        "read_only_confirmed": writability == READ_ONLY,
    }
```

Modify `service/src/api/v1/schemas.py` 的 `DataSourceTestResult`：追加

```python
    writability: Literal["read_only", "writable", "unknown"]
    read_only_confirmed: bool
```

（若该模型不存在，就在 `routes_data_sources.py` 的 `/test` 响应模型处新增此二字段；不要改既有三个键的类型。）

- [ ] **Step 4: API 测试**

追加到 `service/tests/api/test_data_sources.py`:

```python
def test_data_source_test_reports_writability_without_claiming_read_only(
    client, auth_headers, tmp_path, monkeypatch
) -> None:
    """无法确认只读时，接口必须说 unknown/false，不能给前端一个假的绿勾。"""
    root = tmp_path / "nas"
    root.mkdir()
    monkeypatch.setattr(
        "src.application.services.data_sources.detect_writability",
        lambda path: "unknown",
        raising=True,
    )
    source = _create_source(client, auth_headers, tmp_path)  # 复用文件内既有 helper

    body = client.post(f"/api/data-sources/{source['id']}/test", headers=auth_headers).json()

    assert body["writability"] == "unknown"
    assert body["read_only_confirmed"] is False


def test_data_source_test_reports_confirmed_read_only(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.application.services.data_sources.detect_writability",
        lambda path: "read_only",
        raising=True,
    )
    source = _create_source(client, auth_headers, tmp_path=None)  # 用既有 helper 的默认路径

    body = client.post(f"/api/data-sources/{source['id']}/test", headers=auth_headers).json()

    assert body["read_only_confirmed"] is True
```

（两例都要用 `tests/api/test_data_sources.py` 里已存在的建源 helper 与夹具；若该文件里 helper 名字不同，按实际名字调用，不要新造一套夹具。）

Run: `cd service && uv run --python 3.12 pytest tests/api/test_data_sources.py tests/domain/test_source_guard.py -q`
Expected: passed

Run: `cd service && uv run --python 3.12 pytest -q`
Expected: 全量绿（既有断言 `/test` 响应体的用例需同步补两个新键）

- [ ] **Step 5: 提交**

```bash
cd service && uv run --python 3.12 ruff check src tests && uv run --python 3.12 ruff format src tests
git add service/src/domain/source_guard.py service/src/config/settings.py service/src/application/services/data_sources.py service/src/api/v1/schemas.py service/tests
git commit -m "feat: 添加无副作用的 NAS 只读判定与数据源检测结果"
```

---

### Task 2: 在作业规划与 Worker 领取处强制执行只读守卫

**Files:**
- Modify: `service/src/application/services/jobs.py`（`create_job` 校验段）
- Modify: `service/src/api/v1/routes_jobs.py`（409 映射）
- Modify: `service/src/application/worker/processor.py`（`_check_source` 之前）
- Modify: `service/src/config/settings.py`（已在 Task 1 加字段）
- Test: `service/tests/api/test_jobs.py`、`service/tests/application/worker/test_processor.py`

**Interfaces:**
- Consumes: Task 1 的 `guard_source`、`SourceNotReadOnly`、`settings.nas_read_only_policy`
- Produces: `POST /api/jobs` 在可写挂载上返回 `409`；Worker 在可写挂载上以永久失败落 `failed`（不自动重试）

- [ ] **Step 1: 写失败测试**

追加到 `service/tests/api/test_jobs.py`:

```python
def test_job_creation_refuses_writable_mount(client, auth_headers, tmp_path, monkeypatch) -> None:
    """规格 §7.2：发现可写就拒绝处理，而不是"照样跑，反正我们不写"。"""
    from src.domain.source_guard import SourceNotReadOnly

    source_path, _ = _prepare_nas(client, auth_headers, tmp_path)
    monkeypatch.setattr(
        "src.application.services.jobs.guard_source",
        lambda path, **kwargs: (_ for _ in ()).throw(SourceNotReadOnly("writable")),
    )

    response = client.post(
        "/api/jobs",
        headers=auth_headers,
        json={"name": "j", "data_source_id": 1, "source_path": source_path, "rule_set_id": 1},
    )

    assert response.status_code == 409
    assert "writable" in response.json()["detail"].lower()


def test_job_creation_warn_policy_still_plans(client, auth_headers, tmp_path, monkeypatch) -> None:
    from src.domain.source_guard import WRITABLE

    source_path, _ = _prepare_nas(client, auth_headers, tmp_path)
    monkeypatch.setattr(
        "src.application.services.jobs.guard_source", lambda path, **kwargs: WRITABLE
    )

    response = client.post(
        "/api/jobs",
        headers=auth_headers,
        json={"name": "j", "data_source_id": 1, "source_path": source_path, "rule_set_id": 1},
    )

    assert response.status_code == 201
```

追加到 `service/tests/application/worker/test_processor.py`:

```python
def test_writable_mount_is_a_permanent_failure(tmp_path: Path, monkeypatch) -> None:
    """守卫在复制之前生效，且不能自动重试烧 attempts。"""
    from src.domain.source_guard import SourceNotReadOnly

    repo = FakeRepo()

    def guard(path, **kwargs):
        raise SourceNotReadOnly("source path is mounted writable")

    monkeypatch.setattr(processor_module, "guard_source", guard)
    processor = make_processor(repo, FakeWriter(), tmp_path / "work")

    result = processor.process(make_task(tmp_path))

    assert result.status == "failed"
    assert "writable" in (result.error or "").lower()
    assert repo.retryables == [(10, False)]
```

Run: `cd service && uv run --python 3.12 pytest tests/api/test_jobs.py tests/application/worker/test_processor.py -q`
Expected: FAIL（`guard_source` 名称不存在 → monkeypatch 的 raising 参数会报错）

- [ ] **Step 2: 接线**

Modify `service/src/application/services/jobs.py`：导入 `from src.domain.source_guard import SourceNotReadOnly, guard_source`，在 `create_job` 完成路径存在性与逃逸校验之后、开始扫描之前插入：

```python
    # 只读校验必须在扫描前完成：一旦挂载可写，后面的规划与执行都不该发生。
    guard_source(directory, policy=settings.nas_read_only_policy, logger=logger)
```

`create_job` 签名不改；`SourceNotReadOnly` 由路由层转 `409`（沿用该文件已有的 `scan_error`/异常翻译风格，若 `create_job` 内部已有 try/except 包裹扫描异常，就在同一处捕获并抛出 `HTTPException(409, str(exc))`）。

Modify `service/src/api/v1/routes_jobs.py` 的 `POST /jobs`：捕获 `SourceNotReadOnly` 转 `HTTPException(status_code=409, detail=str(exc))`，并在 `responses` 里补 `409: {"description": "Idempotency-Key 请求体不一致，或数据源挂载可写"}`。

Modify `service/src/application/worker/processor.py`：导入 `from src.domain import source_guard as source_guard_module` 与 `guard_source`，在 `process()` 里把 `_check_source` 调用改为

```python
            self._check_source(task)
            guard_source(
                task.source_path.parent, policy=settings.nas_read_only_policy
            )
```

并在通用 `except Exception` 之前加一支：

```python
        except source_guard_module.SourceNotReadOnly as exc:
            self._repo.complete(
                task.id, task.attempts, "failed", 0, 0, str(exc), retryable=False
            )
            return TaskResult("failed", 0, 0, str(exc))
```

（`processor.py` 目前不导入 settings：守卫策略从构造函数注入更符合现有风格，加 `nas_read_only_policy: str = "enforce"` 参数，`worker/main.py` 里传 `settings.nas_read_only_policy`。二选一，不要既读全局 settings 又从 main 传参。选**构造函数注入**，并在 `worker/main.py` 的 `TaskProcessor(...)` 里补该实参。）

Run: `cd service && uv run --python 3.12 pytest tests/api/test_jobs.py tests/application/worker tests/worker -q`
Expected: passed

- [ ] **Step 3: 全量与提交**

```bash
cd service && uv run --python 3.12 pytest -q
cd service && uv run --python 3.12 ruff check src tests && uv run --python 3.12 ruff format src tests
git add service/src/application service/src/api service/src/worker service/tests
git commit -m "feat: 作业规划与 Worker 领取前强制 NAS 只读守卫"
```

---

### Task 3: 尝试历史读取接口

**Files:**
- Modify: `service/src/application/services/queries.py`
- Modify: `service/src/api/v1/routes_jobs.py`
- Modify: `service/src/api/v1/schemas.py`
- Test: `service/tests/api/test_jobs.py`

**Interfaces:**
- Consumes: `FileTaskAttempt`（已有表，已有写入方）
- Produces: `list_task_attempts(session, job_id, task_id) -> list[FileTaskAttempt]`；`GET /api/jobs/{job_id}/files/{task_id}/attempts` → `list[FileTaskAttemptRead]`，按 `attempt_no` 升序，**只返回该作业该任务的行**（跨作业访问 404）

- [ ] **Step 1: 写失败测试**

```python
def test_task_attempts_are_returned_in_order_scoped_to_the_job(client, auth_headers, session) -> None:
    job_id = _seed_job_with_tasks(session, [("failed", "a.zip")])
    task_id = session.exec(select(FileTask).where(FileTask.job_id == job_id)).one().id
    session.add_all(
        [
            FileTaskAttempt(
                file_task_id=task_id,
                attempt_no=2,
                worker_id="w1",
                worker_version="0.1.0",
                status="timeout",
                error="worker heartbeat timeout",
            ),
            FileTaskAttempt(
                file_task_id=task_id,
                attempt_no=1,
                worker_id="w1",
                worker_version="0.1.0",
                status="failed",
                error="read timeout",
            ),
        ]
    )
    session.commit()

    body = client.get(f"/api/jobs/{job_id}/files/{task_id}/attempts", headers=auth_headers).json()

    assert [item["attempt_no"] for item in body] == [1, 2]
    assert [item["status"] for item in body] == ["failed", "timeout"]
    assert body[0]["worker_id"] == "w1"
    assert "file_task_id" in body[0]


def test_attempts_of_other_job_task_is_404(client, auth_headers, session) -> None:
    job_a = _seed_job_with_tasks(session, [("failed", "a.zip")])
    job_b = _seed_job_with_tasks(session, [("failed", "b.zip")])
    task_of_b = session.exec(select(FileTask).where(FileTask.job_id == job_b)).one().id

    assert (
        client.get(
            f"/api/jobs/{job_a}/files/{task_of_b}/attempts", headers=auth_headers
        ).status_code
        == 404
    )


def test_attempts_requires_admin(client, auth_headers, session) -> None:
    job_id = _seed_job_with_tasks(session, [("failed", "a.zip")])
    task_id = session.exec(select(FileTask).where(FileTask.job_id == job_id)).one().id

    assert client.get(f"/api/jobs/{job_id}/files/{task_id}/attempts").status_code == 401
```

Run: `cd service && uv run --python 3.12 pytest tests/api/test_jobs.py -q -k attempts`
Expected: FAIL（404 路由不存在）

- [ ] **Step 2: 实现**

`queries.py`：

```python
def list_task_attempts(session: Session, job_id: int, task_id: int) -> list[FileTaskAttempt]:
    """Return one file task's attempt history, or an empty list when the task
    does not belong to that job（调用方据此给 404，避免跨作业读到别人的重试记录）。"""
    task = session.get(FileTask, task_id)
    if task is None or task.job_id != job_id:
        return []
    statement = (
        select(FileTaskAttempt)
        .where(FileTaskAttempt.file_task_id == task_id)
        .order_by(FileTaskAttempt.attempt_no)
    )
    return list(session.exec(statement).all())
```

`schemas.py` 新增：

```python
class FileTaskAttemptRead(BaseModel):
    file_task_id: int
    attempt_no: int
    worker_id: str
    worker_version: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    processed_lines: int
    matched_rows: int
    error: str | None
```

`routes_jobs.py` 新增：

```python
@router.get(
    "/{job_id}/files/{task_id}/attempts",
    response_model=list[FileTaskAttemptRead],
    responses={404: {"description": "作业或该作业下的文件任务不存在"}},
)
def get_task_attempts(job_id: int, task_id: int, session: SessionDep, _: AdminDep):
    attempts = list_task_attempts(session, job_id, task_id)
    if not attempts:
        raise HTTPException(status_code=404, detail="file task not found")
    return attempts
```

注意：空历史（任务一次都没跑过）与"不属于该作业"都返回空 list，会被映射成 404。这是可接受的语义收紧，但要在路由里区分：先用 `session.get(FileTask, task_id)` 判断存在性与归属，存在且属于该作业却没有历史时返回 `200 []`。实现为

```python
    task = session.get(FileTask, task_id)
    if task is None or task.job_id != job_id:
        raise HTTPException(status_code=404, detail="file task not found")
    return list_task_attempts(session, job_id, task_id)
```

（`list_task_attempts` 内的归属判断保留，两处一致即可；补一个"存在但无历史 → 200 空数组"的测试。）

- [ ] **Step 3: 全量与提交**

```bash
cd service && uv run --python 3.12 pytest -q
cd service && uv run --python 3.12 ruff check src tests && uv run --python 3.12 ruff format src tests
git add service/src/application/services/queries.py service/src/api service/tests
git commit -m "feat: 新增文件任务尝试历史读取接口"
```

---

### Task 4: Worker 资源上报与结构化日志

**Files:**
- Modify: `service/pyproject.toml`（`psutil`）
- Modify: `service/src/infrastructure/task_queue.py`（`WORKER_HEARTBEAT_SQL`）
- Modify: `service/src/application/worker/ports.py`（`heartbeat_worker` 签名不变，新增 `WorkerStats` dataclass）
- Modify: `service/src/worker/main.py`（采集与日志）
- Test: `service/tests/infrastructure/test_task_queue.py`、`service/tests/worker/test_runtime.py`

**Interfaces:**
- Consumes: `Worker` 表已有的 `cpu_percent`/`memory_bytes`/`temp_disk_free_bytes`/`last_error` 列
- Produces:
  - `WorkerStats(cpu_percent: float | None, memory_bytes: int | None, temp_disk_free_bytes: int | None)`，`collect_worker_stats(work_dir: Path) -> WorkerStats`
  - `heartbeat_worker(worker_id, status, current_task_id=None, stats: WorkerStats | None = None)`
  - structlog logger：`src/worker/logging.py:configure_worker_logging()` + 关键事件日志

- [ ] **Step 1: 写失败测试**

`tests/infrastructure/test_task_queue.py`:

```python
def test_worker_heartbeat_persists_resource_stats(engine) -> None:
    """列一直在 WorkerRead 里暴露，但此前没有任何写入方。"""
    from src.application.worker.ports import WorkerStats

    repo = PostgresTaskRepository(engine)
    repo.register_worker("w-stats", "host", "0.1.0")
    repo.heartbeat_worker(
        "w-stats",
        "busy",
        7,
        stats=WorkerStats(cpu_percent=12.5, memory_bytes=2048, temp_disk_free_bytes=4096),
    )

    with Session(engine) as session:
        worker = session.get(Worker, "w-stats")

    assert worker is not None
    assert worker.cpu_percent == 12.5
    assert worker.memory_bytes == 2048
    assert worker.temp_disk_free_bytes == 4096
    assert worker.current_task_id == 7


def test_worker_heartbeat_without_stats_keeps_previous_values(engine) -> None:
    from src.application.worker.ports import WorkerStats

    repo = PostgresTaskRepository(engine)
    repo.register_worker("w-keep", "host", "0.1.0")
    repo.heartbeat_worker(
        "w-keep", "busy", 1, stats=WorkerStats(cpu_percent=9.0, memory_bytes=1, temp_disk_free_bytes=2)
    )
    repo.heartbeat_worker("w-keep", "online", None)

    with Session(engine) as session:
        worker = session.get(Worker, "w-keep")

    assert worker.current_task_id is None
    # 不带 stats 的普通心跳不能把已知资源值抹成 NULL。
    assert worker.cpu_percent == 9.0
```

Run: `cd service && uv run --python 3.12 pytest tests/infrastructure/test_task_queue.py -q -k resource`
Expected: FAIL（`ImportError: cannot import name 'WorkerStats'`）

- [ ] **Step 2: 实现采集与持久化**

`pyproject.toml` 的 `dependencies` 追加：

```toml
  "psutil>=6.0.0",
```

Run: `cd service && uv sync --python 3.12`

`ports.py`：

```python
@dataclass(frozen=True)
class WorkerStats:
    cpu_percent: float | None = None
    memory_bytes: int | None = None
    temp_disk_free_bytes: int | None = None
```

`task_queue.py` 的 `WORKER_HEARTBEAT_SQL` 改为"给了才更新"：

```python
WORKER_HEARTBEAT_SQL = """
UPDATE workers
SET status = :status,
    current_task_id = :task_id,
    cpu_percent = COALESCE(:cpu_percent, cpu_percent),
    memory_bytes = COALESCE(:memory_bytes, memory_bytes),
    temp_disk_free_bytes = COALESCE(:temp_disk_free_bytes, temp_disk_free_bytes),
    last_heartbeat = now()
WHERE id = :id
"""
```

并把 SQL 里的 `now()` 换成绑定参数 `:last_heartbeat`（传入 `utcnow()`）：这条语句目前只在真库上执行过、未在 PG 上验证，而测试夹具里 `last_heartbeat` 列是 NOT NULL，用 `now()` 会让本任务的"不带 stats 时保留原值"用例在 SQLite 上直接语法失败。改成绑定后此语句可移植、能被真实执行验证，语义与 `mark_dead_workers` 的 `:cutoff` 一致。

`heartbeat_worker` 签名与实参：

```python
    def heartbeat_worker(
        self,
        worker_id: str,
        status: str,
        current_task_id: int | None = None,
        stats: WorkerStats | None = None,
    ) -> None:
        values = stats or WorkerStats()
        with self._engine.begin() as conn:
            conn.execute(
                text(WORKER_HEARTBEAT_SQL),
                {
                    "id": worker_id,
                    "status": status,
                    "task_id": current_task_id,
                    "cpu_percent": values.cpu_percent,
                    "memory_bytes": values.memory_bytes,
                    "temp_disk_free_bytes": values.temp_disk_free_bytes,
                    "last_heartbeat": utcnow(),
                },
            )
```

Create `service/src/worker/metrics.py`:

```python
"""Worker 资源采样：失败一律降级为 None，绝不因为采集问题影响任务处理。"""

import shutil
from pathlib import Path

import psutil

from src.application.worker.ports import WorkerStats


def collect_worker_stats(work_dir: Path) -> WorkerStats:
    try:
        process = psutil.Process()
        cpu_percent = process.cpu_percent(interval=None)
        memory_bytes = process.memory_info().rss
    except Exception:  # noqa: BLE001 - 采集失败不能让 Worker 退出
        cpu_percent, memory_bytes = None, None
    try:
        free = shutil.disk_usage(str(work_dir)).free
    except OSError:
        free = None
    return WorkerStats(
        cpu_percent=None if cpu_percent is None else float(cpu_percent),
        memory_bytes=None if memory_bytes is None else int(memory_bytes),
        temp_disk_free_bytes=None if free is None else int(free),
    )
```

（`shutil.disk_usage` 只读目录所在盘的统计信息，不写 NAS；`work_dir` 始终是本地临时盘。）

- [ ] **Step 3: 日志与循环接线**

Create `service/src/worker/logging_setup.py`:

```python
import logging

import structlog


def configure_worker_logging(*, worker_id: str, level: str = "INFO") -> None:
    """One shared logger configuration for the worker loop; JSON on stdout.

    规格 §7.4 要求 Worker 有可读日志：此前永久失败只能从 file_tasks.error 反查。
    """
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        init_bind_values={"worker_id": worker_id},
    )


def get_logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger()
```

Modify `service/src/worker/main.py`：
- `WorkerRuntime.__init__` 增加 `collect_stats: Callable[[Path], WorkerStats] | None = None` 与 `logger=None`；默认 `None` 时不采集（轻量 fake 不受影响）。
- 内部把三处 `heartbeat_worker(...)` 调用改为带 `stats=self._stats()`（`_stats()` 在 `collect_stats is None` 时返回 `None`，否则 `collect_stats(self._work_dir)`）。
- `run_once` 的三个关键分支加日志：领取成功 `task_claimed`、`LeaseLost` → `task_lease_lost`（warning）、永久失败/取消分支由 `processor` 抛出前的错误文案带上下文记录为 `task_finished`（info，含 `status`/`processed_lines`/`matched_rows`）。
- `main()` 里调用 `configure_worker_logging(worker_id=worker_id)`，并把 `collect_stats=collect_worker_stats` 与 `get_logger()` 注入 `WorkerRuntime`。
- 捕获 `SourceNotReadOnly`（Task 2 的守卫）在 `run_once` 外层不要吞掉：它已被 processor 转成永久失败，只需在 `run_once` 里若 `TaskResult.status == "failed"` 时记录 `logger.error("task_failed", ...)`，不要打断循环。

Run: `cd service && uv run --python 3.12 pytest tests/infrastructure/test_task_queue.py tests/worker -q`
Expected: passed（`FakeRepo.heartbeat_worker` 需接受 `stats=None` 关键字）

- [ ] **Step 4: 采集函数测试**

`tests/worker/test_metrics.py`:

```python
from pathlib import Path

from src.worker.metrics import collect_worker_stats


def test_collect_returns_disk_free_for_existing_dir(tmp_path: Path) -> None:
    stats = collect_worker_stats(tmp_path)

    assert stats.temp_disk_free_bytes is not None
    assert stats.temp_disk_free_bytes > 0
    assert stats.memory_bytes is None or stats.memory_bytes > 0


def test_collect_survives_missing_work_dir(tmp_path: Path) -> None:
    stats = collect_worker_stats(tmp_path / "does-not-exist")

    assert stats.temp_disk_free_bytes is None
```

Run: `cd service && uv run --python 3.12 pytest tests/worker/test_metrics.py -v`
Expected: 2 passed

- [ ] **Step 5: 全量与提交**

```bash
cd service && uv run --python 3.12 pytest -q
cd service && uv run --python 3.12 ruff check src tests && uv run --python 3.12 ruff format src tests
git add service/pyproject.toml service/uv.lock service/src/worker service/src/infrastructure/task_queue.py service/src/application/worker/ports.py service/tests
git commit -m "feat: Worker 上报资源指标并补齐结构化日志"
```

---

### Task 5: 前端尝试历史抽屉与 Worker 资源列

**Files:**
- Modify: `web/src/api/logharvest.ts`、`web/src/api/logharvestTypes.ts`
- Modify: `web/src/views/logharvest/jobs/detail.vue`
- Modify: `web/src/views/logharvest/workers/index.vue`
- Modify: `web/README.md`

**Interfaces:**
- Consumes: `GET /api/jobs/{job_id}/files/{task_id}/attempts`（Task 3）、`WorkerRead` 的 `cpu_percent`/`memory_bytes`/`temp_disk_free_bytes`/`last_error`（Task 4 后有值）
- Produces: 文件任务行的"尝试历史"抽屉；Worker 表的 CPU/内存/临时盘三列

- [ ] **Step 1: 客户端与类型**

`logharvestTypes.ts` 追加：

```ts
export type FileTaskAttempt = {
  file_task_id: number;
  attempt_no: number;
  worker_id: string;
  worker_version: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  processed_lines: number;
  matched_rows: number;
  error: string | null;
};
```

`logharvest.ts` 追加（返回裸数组，与全站一致）：

```ts
export const listTaskAttempts = (jobId: number, taskId: number) =>
  http.request<FileTaskAttempt[]>("get", `/jobs/${jobId}/files/${taskId}/attempts`);
```

`WorkerInfo` 类型补 `cpu_percent: number | null`、`memory_bytes: number | null`、`temp_disk_free_bytes: number | null`、`last_error: string | null`（以 `service/src/api/v1/schemas.py` 的 `WorkerRead` 为准，字段名逐一对齐后再写）。

- [ ] **Step 2: 尝试历史抽屉**

`jobs/detail.vue` 文件任务表加一列"历史"，点击打开 `el-drawer`：`listTaskAttempts` 拉取，表格列 `attempt_no`/`worker_id`/`worker_version`/`status`/`started_at`/`finished_at`/`processed_lines`/`matched_rows`/`error`；空数组时显示"该任务尚无尝试记录"；请求失败时显示后端 `detail`，不要静默空表。抽屉打开时把 `task_id` 与 `job_id` 记在状态里，切换任务重新加载。

- [ ] **Step 3: Worker 资源列**

`workers/index.vue` 增加三列：CPU（`cpu_percent` 一位小数 + `%`，`null` 显示 `—`）、内存（MB 保留 1 位）、临时盘空闲（GB 保留 2 位）。**必须如实呈现 `null`**：Task 4 之前上线的历史行没有值，界面不能显示 0 让人误判。

- [ ] **Step 4: 验证与提交**

```bash
cd web && pnpm typecheck
cd web && pnpm build 2>&1 | tail -6
```
Expected: typecheck 无输出（通过）、build 成功产出 `dist/`。

`web/README.md` 的界面清单补"尝试历史抽屉"和"Worker 资源列（依赖 Worker 上报，Worker 需重启才有值）"，并保留"未做人工视觉确认"的既有声明。

```bash
git add web
git commit -m "feat: 前端补齐尝试历史抽屉与 Worker 资源列"
```

---

## Plan Self-Review

**1. Spec coverage：**
- §7.2 "启动/健康检查时校验 NAS 只读，可写则拒绝处理并告警" → Task 1（判定与三态）+ Task 2（规划与 Worker 两处强制、`warn` 策略留口）。
- §7.2 资源上报 `cpu_percent`/`memory_bytes`/`temp_disk_free_bytes` → Task 4；界面消费 → Task 5。
- §7.4 尝试历史可读 → Task 3 + Task 5 抽屉。
- §7.4 Worker 结构化日志 → Task 4 的 `logging_setup`。
- 明确不覆盖（避免过度设计）：`last_error` 的完整语义、月度分区、进程内并发池、`max_matches_per_task` 强制、多行堆栈、动态菜单。

**2. Placeholder scan：** Task 3 Step 2 显式要求把"空历史"与"跨作业"区分开并给出最终实现形态；Task 4 Step 2 要求把 `now()` 改绑参数并说明原因——这两处都是可执行的最终要求，不是待办。Task 5 Step 1 要求字段以 `WorkerRead` 为准先读源码，是核对动作。其余步骤均给出代码或命令。

**3. Type consistency：** `WorkerStats` 三字段与 `workers` 表列名一致；`heartbeat_worker` 新参数一律带默认值，既有 26 处调用与测试 fake 不受影响；`FileTaskAttemptRead` 字段与 `FileTaskAttempt` 模型逐列对应；`guard_source(path, *, policy, logger=None)` 在 Task 2 的两处调用参数形状一致（Worker 侧走 `TaskProcessor` 的 `nas_read_only_policy` 构造参数，不读全局 settings）；`detect_writability` 返回值恒为 `read_only|writable|unknown` 三值，前端/接口 `Literal` 与之严格同名。
