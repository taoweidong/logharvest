# LogHarvest M0 核心引擎实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 LogHarvest 的可测试核心引擎：规则编译与执行、压缩包流式读取、Worker 任务处理、7 张核心表的 SQLModel 模型，以及 FastAPI 健康检查骨架。

**Architecture:** 在 `service/` 下按 DDD 分层建立最小 Python 服务。规则引擎和归档读取是纯领域模块；Worker 通过协议依赖任务仓储和结果写入器，测试使用内存 fake；SQLModel 模型只负责持久化结构，M0 不接 PostgreSQL 队列和前端。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLModel、psycopg 3、`regex`、pytest、uv。

**Spec:**
- [主设计方案](../specs/2026-10-24-compressed-log-extraction-design.md)
- [技术栈选型评估](../specs/2026-10-24-technology-selection-evaluation.md)

## Global Constraints

- Python 3.12+；依赖用 `uv` 管理。
- 任务队列最终使用 PostgreSQL `file_tasks` + `SKIP LOCKED`，本阶段不实现 PG 队列。
- Worker 不绑定作业；完成一个任务后继续领取；本阶段用 fake 仓储验证处理器。
- NAS 原始数据只读；本阶段只读取本地测试压缩包，不写源文件。
- 单文件按 10 万行级设计；流式读取，不整包加载内存。
- 单机 16GB 内存；Worker 默认 4、最多 6；本阶段不实现并发调度。
- 不引入 Celery、Redis 任务队列、Elasticsearch、ClickHouse、Kafka。
- 所有新增代码必须有测试；提交信息用中文，`feat/fix/docs/test` 前缀。

---

## Agent 执行策略

| 阶段 | 执行者 | 任务 | 说明 |
|---|---|---|---|
| Phase 0 | 主 Agent | Task 1 服务骨架 | 先建立 pyproject、配置、FastAPI 健康检查和测试入口 |
| Phase 1 | Agent Rules | Task 2 规则引擎 | 与 Task 3/5 文件不重叠，可并行 |
| Phase 1 | Agent Archive | Task 3 归档读取 | 与 Task 2/5 文件不重叠，可并行 |
| Phase 1 | Agent Data | Task 5 SQLModel 模型 | 与 Task 2/3 文件不重叠，可并行 |
| Phase 2 | Agent Worker | Task 4 Worker 处理器 | 依赖 Task 2/3 接口，最后执行 |
| Phase 3 | 主 Agent | Task 6 集成与文档 | 跑全量测试、ruff、提交 |
| Phase 3 | Reviewer Agent | 代码审查 | 检查接口一致性、边界条件、测试覆盖 |

文件所有权：

- Task 1：`service/pyproject.toml`、`service/src/main.py`、`service/src/config/`、`service/src/api/v1/router.py`、`service/tests/test_health.py`
- Task 2：`service/src/domain/rules/`、`service/tests/domain/rules/`
- Task 3：`service/src/domain/archive/`、`service/tests/domain/archive/`
- Task 4：`service/src/application/worker/`、`service/tests/application/worker/`
- Task 5：`service/src/domain/models/`、`service/src/infrastructure/`、`service/tests/domain/models/`
- Task 6：`README.md`、`service/README.md`、`.env.example`

除上述目录外，任何 Agent 不得修改其他文件；`service/src/__init__.py`、`service/src/domain/__init__.py` etc. 由 Task 1 创建。

---

### Task 1: 服务骨架与健康检查

**Files:**
- Create: `service/pyproject.toml`
- Create: `service/src/__init__.py`
- Create: `service/src/main.py`
- Create: `service/src/config/__init__.py`
- Create: `service/src/config/settings.py`
- Create: `service/src/api/__init__.py`
- Create: `service/src/api/v1/__init__.py`
- Create: `service/src/api/v1/router.py`
- Create: `service/tests/test_health.py`
- Create: `service/src/domain/__init__.py`
- Create: `service/src/application/__init__.py`
- Create: `service/src/infrastructure/__init__.py`

**Interfaces:**
- Produces: `src.main.create_app() -> FastAPI`
- Produces: `src.api.v1.router.api_router: APIRouter`
- Produces: `src.config.settings.settings: Settings`
- Consumes: none

- [ ] **Step 1: Write the failing test**

`service/tests/test_health.py`:

```python
from fastapi.testclient import TestClient

from src.main import create_app


def test_health_returns_ok() -> None:
    client = TestClient(create_app())
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "logharvest"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd service && uv run --python 3.12 pytest tests/test_health.py -v`

Expected: FAIL because `service/pyproject.toml` and `src.main` do not exist.

- [ ] **Step 3: Write minimal implementation**

`service/pyproject.toml`:

```toml
[project]
name = "logharvest-service"
version = "0.1.0"
description = "LogHarvest compressed log rule extraction service"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115.0",
  "uvicorn[standard]>=0.30.0",
  "pydantic>=2.8.0",
  "pydantic-settings>=2.4.0",
  "sqlmodel>=0.0.22",
  "psycopg[binary]>=3.2.0",
  "regex>=2024.5.15",
  "orjson>=3.10.0",
  "structlog>=24.1.0",
  "prometheus-client>=0.20.0",
]

[dependency-groups]
dev = [
  "pytest>=8.2.0",
  "pytest-asyncio>=0.23.0",
  "httpx>=0.27.0",
  "hypothesis>=6.100.0",
  "ruff>=0.5.0",
  "mypy>=1.10.0",
]

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
line-length = 100
target-version = "py312"
```

`service/src/config/settings.py`:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LOGHARVEST_",
        extra="ignore",
    )

    app_name: str = "LogHarvest"
    environment: str = "dev"
    database_url: str = "sqlite:///./logharvest.db"
    auto_create_tables: bool = True
    worker_poll_seconds: float = 1.0
    worker_version: str = "0.1.0"
    max_concurrent_copies: int = 4
    matches_batch_size: int = 1000
    max_matches_per_task: int = 200_000


settings = Settings()
```

`service/src/api/v1/router.py`:

```python
from fastapi import APIRouter

api_router = APIRouter()


@api_router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "logharvest"}
```

`service/src/main.py`:

```python
from fastapi import FastAPI

from src.api.v1.router import api_router
from src.config.settings import settings


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name)
    app.include_router(api_router, prefix="/api")
    return app


app = create_app()
```

Create empty `__init__.py` files in:
`service/src/`, `service/src/config/`, `service/src/api/`, `service/src/api/v1/`, `service/src/domain/`, `service/src/application/`, `service/src/infrastructure/`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd service && uv run --python 3.12 pytest tests/test_health.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add service/pyproject.toml service/src service/tests/test_health.py
git commit -m "feat: 初始化 LogHarvest 服务骨架"
```

---

### Task 2: 规则引擎

**Files:**
- Create: `service/src/domain/rules/__init__.py`
- Create: `service/src/domain/rules/models.py`
- Create: `service/src/domain/rules/evaluator.py`
- Create: `service/tests/domain/rules/test_evaluator.py`

**Interfaces:**
- Produces: `RuleSetConfig`, `Rule`, `KeywordMatch`, `RegexMatch`, `Threshold`, `RuleMatch`
- Produces: `compile_ruleset(config: RuleSetConfig) -> CompiledRuleSet`
- Produces: `evaluate_line(compiled: CompiledRuleSet, line: str, line_no: int) -> list[RuleMatch]`
- Consumes: none

- [ ] **Step 1: Write the failing test**

`service/tests/domain/rules/test_evaluator.py`:

```python
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.domain.rules.evaluator import compile_ruleset, evaluate_line
from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleSetConfig,
    Threshold,
)


def compile_one(rule: Rule):
    return compile_ruleset(RuleSetConfig(name="t", rules=[rule]))


def test_keyword_any_is_case_insensitive_by_default() -> None:
    compiled = compile_one(Rule(id="e", match=KeywordMatch(any=["error", "exception"])))
    matches = evaluate_line(compiled, "an ERROR happened", 1)
    assert [m.rule_id for m in matches] == ["e"]


def test_keyword_all_requires_every_keyword() -> None:
    compiled = compile_one(Rule(id="e", match=KeywordMatch(all=["error", "timeout"])))
    assert evaluate_line(compiled, "error timeout", 1)
    assert evaluate_line(compiled, "error only", 2) == []


def test_regex_extracts_named_fields_and_threshold() -> None:
    compiled = compile_one(
        Rule(
            id="slow",
            match=RegexMatch(pattern=r"elapsed=(?P<elapsed_ms>\d+)ms"),
            threshold=Threshold(field="elapsed_ms", op="gt", value=Decimal("5000")),
            severity="warning",
        )
    )
    matches = evaluate_line(compiled, "request elapsed=6000ms", 3)
    assert len(matches) == 1
    assert matches[0].fields["elapsed_ms"] == "6000"
    assert matches[0].numeric_value == Decimal("6000")
    assert matches[0].severity == "warning"
    assert evaluate_line(compiled, "request elapsed=100ms", 4) == []


def test_threshold_missing_field_does_not_match() -> None:
    compiled = compile_one(
        Rule(
            id="slow",
            match=RegexMatch(pattern=r"elapsed=(?P<elapsed_ms>\d+)ms"),
            threshold=Threshold(field="missing", op="gt", value=Decimal("1")),
        )
    )
    assert evaluate_line(compiled, "elapsed=6000ms", 1) == []


def test_disabled_rule_is_skipped() -> None:
    compiled = compile_one(Rule(id="e", enabled=False, match=KeywordMatch(any=["error"])))
    assert evaluate_line(compiled, "error", 1) == []


def test_keyword_any_and_all_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        KeywordMatch(any=["a"], all=["b"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd service && uv run --python 3.12 pytest tests/domain/rules/test_evaluator.py -v`

Expected: FAIL because `src.domain.rules` does not exist.

- [ ] **Step 3: Write minimal implementation**

`service/src/domain/rules/models.py`:

```python
from decimal import Decimal
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator


class KeywordMatch(BaseModel):
    type: Literal["keyword"] = "keyword"
    any: list[str] = Field(default_factory=list)
    all: list[str] = Field(default_factory=list)
    case_sensitive: bool = False

    @model_validator(mode="after")
    def validate_one_mode(self) -> "KeywordMatch":
        if bool(self.any) == bool(self.all):
            raise ValueError("exactly one of any/all must be provided")
        return self


class RegexMatch(BaseModel):
    type: Literal["regex"] = "regex"
    pattern: str
    flags: str = ""
    timeout_ms: int = 50


MatchSpec = Annotated[Union[KeywordMatch, RegexMatch], Field(discriminator="type")]


class Threshold(BaseModel):
    field: str
    op: Literal["gt", "gte", "lt", "lte", "eq"]
    value: Decimal


class Rule(BaseModel):
    id: str
    name: str = ""
    enabled: bool = True
    match: MatchSpec
    threshold: Threshold | None = None
    severity: str = "info"
    tags: list[str] = Field(default_factory=list)


class RuleSetConfig(BaseModel):
    name: str
    file_glob: str = "**/*"
    encoding: str = "utf-8"
    fallback_encoding: str = "gb18030"
    rules: list[Rule]

    @model_validator(mode="after")
    def validate_unique_rule_ids(self) -> "RuleSetConfig":
        ids = [rule.id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        return self


class RuleMatch(BaseModel):
    rule_id: str
    severity: str
    tags: list[str]
    fields: dict[str, str | None]
    numeric_value: Decimal | None = None
    matched_text: str | None = None
```

`service/src/domain/rules/evaluator.py`:

```python
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable

import regex

from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleMatch,
    RuleSetConfig,
    Threshold,
)

_OPS: dict[str, Callable[[Decimal, Decimal], bool]] = {
    "gt": lambda left, right: left > right,
    "gte": lambda left, right: left >= right,
    "lt": lambda left, right: left < right,
    "lte": lambda left, right: left <= right,
    "eq": lambda left, right: left == right,
}


@dataclass(frozen=True)
class CompiledRule:
    rule: Rule
    regex: regex.Pattern | None = None


@dataclass(frozen=True)
class CompiledRuleSet:
    config: RuleSetConfig
    rules: tuple[CompiledRule, ...]


def _regex_flags(flags: str) -> int:
    result = 0
    if "i" in flags:
        result |= regex.IGNORECASE
    if "m" in flags:
        result |= regex.MULTILINE
    if "s" in flags:
        result |= regex.DOTALL
    return result


def compile_ruleset(config: RuleSetConfig) -> CompiledRuleSet:
    compiled: list[CompiledRule] = []
    for rule in config.rules:
        if isinstance(rule.match, RegexMatch):
            pattern = regex.compile(rule.match.pattern, _regex_flags(rule.match.flags))
            compiled.append(CompiledRule(rule=rule, regex=pattern))
        else:
            compiled.append(CompiledRule(rule=rule))
    return CompiledRuleSet(config=config, rules=tuple(compiled))


def _threshold_matches(threshold: Threshold, fields: dict[str, str | None]) -> tuple[bool, Decimal | None]:
    raw = fields.get(threshold.field)
    if raw is None:
        return False, None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return False, None
    return _OPS[threshold.op](value, threshold.value), value


def evaluate_line(compiled: CompiledRuleSet, line: str, line_no: int) -> list[RuleMatch]:
    del line_no  # line number is attached by the worker when persisting
    matches: list[RuleMatch] = []
    for item in compiled.rules:
        rule = item.rule
        if not rule.enabled:
            continue

        fields: dict[str, str | None] = {}
        matched_text: str | None = None

        if isinstance(rule.match, KeywordMatch):
            haystack = line if rule.match.case_sensitive else line.lower()
            words = rule.match.any or rule.match.all
            needles = words if rule.match.case_sensitive else [word.lower() for word in words]
            if rule.match.any:
                hit = next((word for word in needles if word in haystack), None)
                if hit is None:
                    continue
                matched_text = hit
            else:
                if not all(word in haystack for word in needles):
                    continue
                matched_text = words[0]
        elif isinstance(rule.match, RegexMatch):
            assert item.regex is not None
            found = item.regex.search(line, timeout=rule.match.timeout_ms / 1000)
            if found is None:
                continue
            fields = found.groupdict()
            matched_text = found.group(0)
        else:
            continue

        numeric_value: Decimal | None = None
        if rule.threshold is not None:
            passed, numeric_value = _threshold_matches(rule.threshold, fields)
            if not passed:
                continue

        matches.append(
            RuleMatch(
                rule_id=rule.id,
                severity=rule.severity,
                tags=list(rule.tags),
                fields=fields,
                numeric_value=numeric_value,
                matched_text=matched_text,
            )
        )
    return matches
```

`service/src/domain/rules/__init__.py`:

```python
from src.domain.rules.evaluator import CompiledRuleSet, compile_ruleset, evaluate_line
from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleMatch,
    RuleSetConfig,
    Threshold,
)

__all__ = [
    "CompiledRuleSet",
    "KeywordMatch",
    "RegexMatch",
    "Rule",
    "RuleMatch",
    "RuleSetConfig",
    "Threshold",
    "compile_ruleset",
    "evaluate_line",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd service && uv run --python 3.12 pytest tests/domain/rules/test_evaluator.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add service/src/domain/rules service/tests/domain/rules
git commit -m "feat: 实现日志规则编译与匹配引擎"
```

---

### Task 3: 压缩包发现与流式读取

**Files:**
- Create: `service/src/domain/archive/__init__.py`
- Create: `service/src/domain/archive/models.py`
- Create: `service/src/domain/archive/reader.py`
- Create: `service/tests/domain/archive/test_reader.py`

**Interfaces:**
- Produces: `LineRecord(line_no, text, member_path, file_name, truncated)`
- Produces: `discover_archives(source_path: Path, recursive: bool = True, max_depth: int = 5, suffixes: set[str] | None = None) -> list[Path]`
- Produces: `iter_archive_lines(path: Path, encoding: str = "utf-8", fallback_encoding: str = "gb18030", max_line_bytes: int = 1_048_576) -> Iterator[LineRecord]`
- Consumes: none

- [ ] **Step 1: Write the failing test**

`service/tests/domain/archive/test_reader.py`:

```python
import gzip
import tarfile
import zipfile

from src.domain.archive.reader import discover_archives, iter_archive_lines


def test_discover_archives_is_recursive_and_skips_hidden(tmp_path) -> None:
    root = tmp_path / "logs"
    nested = root / "2026" / "10"
    nested.mkdir(parents=True)
    (root / "a.zip").write_bytes(b"zip")
    (nested / "b.log.gz").write_bytes(b"gz")
    (nested / "ignore.tmp").write_text("x")
    hidden = root / ".hidden"
    hidden.mkdir()
    (hidden / "c.zip").write_bytes(b"zip")

    found = discover_archives(root)
    assert [path.name for path in found] == ["a.zip", "b.log.gz"]


def test_zip_members_and_line_numbers(tmp_path) -> None:
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("app.log", "ERROR one\nelapsed=6000ms\n")
        zf.writestr("other.log", "ok\n")

    records = list(iter_archive_lines(archive))
    assert [(r.member_path, r.line_no, r.text) for r in records] == [
        ("app.log", 1, "ERROR one"),
        ("app.log", 2, "elapsed=6000ms"),
        ("other.log", 1, "ok"),
    ]


def test_tar_gz_member_is_streamed(tmp_path) -> None:
    archive = tmp_path / "a.tar.gz"
    source = tmp_path / "app.log"
    source.write_text("line1\nline2\n")
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source, arcname="nested/app.log")

    records = list(iter_archive_lines(archive))
    assert [(r.member_path, r.line_no) for r in records] == [
        ("nested/app.log", 1),
        ("nested/app.log", 2),
    ]


def test_single_gzip_and_raw_text(tmp_path) -> None:
    gz = tmp_path / "app.log.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as stream:
        stream.write("a\nb\n")
    assert [r.text for r in iter_archive_lines(gz)] == ["a", "b"]

    raw = tmp_path / "app.log"
    raw.write_text("x\ny\n", encoding="utf-8")
    assert [r.text for r in iter_archive_lines(raw)] == ["x", "y"]


def test_gbk_fallback_and_long_line_truncation(tmp_path) -> None:
    raw = tmp_path / "app.log"
    raw.write_bytes("错误\n".encode("gb18030"))
    records = list(iter_archive_lines(raw, encoding="utf-8", fallback_encoding="gb18030"))
    assert records[0].text == "错误"

    long_line = tmp_path / "long.log"
    long_line.write_text("x" * 50 + "\nnext\n", encoding="utf-8")
    records = list(iter_archive_lines(long_line, max_line_bytes=10))
    assert records[0].truncated is True
    assert records[1].text == "next"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd service && uv run --python 3.12 pytest tests/domain/archive/test_reader.py -v`

Expected: FAIL because `src.domain.archive` does not exist.

- [ ] **Step 3: Write minimal implementation**

`service/src/domain/archive/models.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class LineRecord:
    line_no: int
    text: str
    member_path: str
    file_name: str
    truncated: bool = False
```

`service/src/domain/archive/reader.py`:

```python
import bz2
import gzip
import lzma
import os
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

from src.domain.archive.models import LineRecord

ARCHIVE_SUFFIXES = {
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz", ".txz", ".log", ".txt",
}
_COMPRESSED_SINGLE = {".gz", ".bz2", ".xz"}


def discover_archives(
    source_path: Path,
    recursive: bool = True,
    max_depth: int = 5,
    suffixes: set[str] | None = None,
) -> list[Path]:
    root = Path(source_path)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    allowed = suffixes or ARCHIVE_SUFFIXES
    found: list[Path] = []

    if not recursive:
        for path in sorted(root.iterdir()):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in allowed:
                found.append(path)
        return found

    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".") and not (current_path / name).is_symlink()
        )
        if depth >= max_depth:
            dirnames[:] = []
        for name in sorted(filenames):
            path = current_path / name
            if path.is_symlink() or path.suffix.lower() not in allowed:
                continue
            found.append(path)
    return found


def _decode(raw: bytes, encoding: str, fallback_encoding: str) -> str:
    for candidate in (encoding, fallback_encoding):
        try:
            return raw.decode(candidate)
        except UnicodeDecodeError:
            continue
    return raw.decode(encoding, errors="replace")


def _iter_binary_lines(
    stream,
    member_path: str,
    file_name: str,
    encoding: str,
    fallback_encoding: str,
    max_line_bytes: int,
) -> Iterator[LineRecord]:
    line_no = 0
    while True:
        raw = stream.readline(max_line_bytes + 1)
        if not raw:
            return
        truncated = False
        if len(raw) > max_line_bytes and not raw.endswith(b"\n"):
            raw = raw[:max_line_bytes]
            truncated = True
            while True:
                rest = stream.readline(8192)
                if not rest or rest.endswith(b"\n"):
                    break
        line_no += 1
        yield LineRecord(
            line_no=line_no,
            text=_decode(raw.rstrip(b"\r\n"), encoding, fallback_encoding),
            member_path=member_path,
            file_name=file_name,
            truncated=truncated,
        )


def _iter_zip(path: Path, encoding: str, fallback_encoding: str, max_line_bytes: int):
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            with archive.open(info) as stream:
                yield from _iter_binary_lines(
                    stream,
                    info.filename,
                    Path(info.filename).name,
                    encoding,
                    fallback_encoding,
                    max_line_bytes,
                )


def _iter_tar_or_single(path: Path, encoding: str, fallback_encoding: str, max_line_bytes: int):
    try:
        with tarfile.open(path, mode="r|*") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                with stream:
                    yield from _iter_binary_lines(
                        stream,
                        member.name,
                        Path(member.name).name,
                        encoding,
                        fallback_encoding,
                        max_line_bytes,
                    )
        return
    except tarfile.ReadError:
        pass

    opener = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}[path.suffix.lower()]
    with opener(path, "rb") as stream:
        yield from _iter_binary_lines(
            stream,
            path.stem,
            path.stem,
            encoding,
            fallback_encoding,
            max_line_bytes,
        )


def iter_archive_lines(
    path: Path,
    encoding: str = "utf-8",
    fallback_encoding: str = "gb18030",
    max_line_bytes: int = 1_048_576,
) -> Iterator[LineRecord]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".zip":
        yield from _iter_zip(path, encoding, fallback_encoding, max_line_bytes)
    elif suffix in {".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz", ".txz"}:
        yield from _iter_tar_or_single(path, encoding, fallback_encoding, max_line_bytes)
    elif suffix in {".log", ".txt"}:
        with path.open("rb") as stream:
            yield from _iter_binary_lines(
                stream,
                path.name,
                path.name,
                encoding,
                fallback_encoding,
                max_line_bytes,
            )
    else:
        raise ValueError(f"unsupported archive type: {path}")
```

`service/src/domain/archive/__init__.py`:

```python
from src.domain.archive.models import LineRecord
from src.domain.archive.reader import discover_archives, iter_archive_lines

__all__ = ["LineRecord", "discover_archives", "iter_archive_lines"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd service && uv run --python 3.12 pytest tests/domain/archive/test_reader.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add service/src/domain/archive service/tests/domain/archive
git commit -m "feat: 实现压缩包发现与流式行读取"
```

---

### Task 4: Worker 任务处理器

**Files:**
- Create: `service/src/application/worker/__init__.py`
- Create: `service/src/application/worker/ports.py`
- Create: `service/src/application/worker/processor.py`
- Create: `service/tests/application/worker/test_processor.py`

**Interfaces:**
- Consumes: `LineRecord`, `iter_archive_lines` from Task 3
- Consumes: `RuleSetConfig`, `compile_ruleset`, `evaluate_line` from Task 2
- Produces: `ClaimedTask`, `MatchRecord`, `TaskResult`, `TaskRepository`, `MatchWriter`, `TaskProcessor.process(task) -> TaskResult`

- [ ] **Step 1: Write the failing test**

`service/tests/application/worker/test_processor.py`:

```python
import zipfile
from decimal import Decimal
from pathlib import Path

from src.application.worker.ports import ClaimedTask, MatchRecord
from src.application.worker.processor import TaskProcessor
from src.domain.rules.evaluator import compile_ruleset
from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleSetConfig,
    Threshold,
)


class FakeRepo:
    def __init__(self) -> None:
        self.heartbeats: list[tuple[int, int]] = []
        self.completed: list[tuple[int, str, int, int, str | None]] = []

    def heartbeat(self, task_id: int, attempts: int, processed_lines: int, matched_rows: int) -> bool:
        self.heartbeats.append((processed_lines, matched_rows))
        return True

    def complete(self, task_id: int, attempts: int, status: str, processed_lines: int, matched_rows: int, error: str | None = None) -> None:
        self.completed.append((task_id, status, processed_lines, matched_rows, error))


class FakeWriter:
    def __init__(self) -> None:
        self.deleted: list[int] = []
        self.written: list[MatchRecord] = []

    def delete_for_task(self, task_id: int) -> None:
        self.deleted.append(task_id)

    def write_batch(self, matches) -> None:
        self.written.extend(matches)


def make_task(tmp_path: Path, attempts: int = 1) -> ClaimedTask:
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("app.log", "ERROR one\nelapsed=6000ms\n")
    return ClaimedTask(
        id=10,
        job_id=20,
        source_path=archive,
        file_name="a.zip",
        attempts=attempts,
        rule_set_id=30,
        rule_version=1,
    )


def make_rules():
    return compile_ruleset(
        RuleSetConfig(
            name="t",
            rules=[
                Rule(id="error", match=KeywordMatch(any=["ERROR"]), severity="error"),
                Rule(
                    id="slow",
                    match=RegexMatch(pattern=r"elapsed=(?P<elapsed_ms>\d+)ms"),
                    threshold=Threshold(field="elapsed_ms", op="gt", value=Decimal("5000")),
                    severity="warning",
                ),
            ],
        )
    )


def test_processor_reads_archive_and_writes_matches(tmp_path: Path) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    processor = TaskProcessor(
        repo=repo,
        writer=writer,
        rules=make_rules(),
        work_dir=tmp_path / "work",
        batch_size=1,
        heartbeat_every_lines=1,
    )

    result = processor.process(make_task(tmp_path))

    assert result.status == "succeeded"
    assert result.processed_lines == 2
    assert result.matched_rows == 2
    assert [(m.rule_id, m.member_path, m.line_no) for m in writer.written] == [
        ("error", "app.log", 1),
        ("slow", "app.log", 2),
    ]
    assert writer.written[1].fields["elapsed_ms"] == "6000"
    assert repo.completed == [(10, "succeeded", 2, 2, None)]


def test_retry_deletes_old_matches(tmp_path: Path) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    processor = TaskProcessor(repo=repo, writer=writer, rules=make_rules(), work_dir=tmp_path / "work")
    processor.process(make_task(tmp_path, attempts=2))
    assert writer.deleted == [10]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd service && uv run --python 3.12 pytest tests/application/worker/test_processor.py -v`

Expected: FAIL because `src.application.worker` does not exist.

- [ ] **Step 3: Write minimal implementation**

`service/src/application/worker/ports.py`:

```python
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class ClaimedTask:
    id: int
    job_id: int
    source_path: Path
    file_name: str
    attempts: int
    rule_set_id: int
    rule_version: int
    encoding: str = "utf-8"
    fallback_encoding: str = "gb18030"


@dataclass(frozen=True)
class MatchRecord:
    job_id: int
    file_task_id: int
    rule_id: str
    rule_set_id: int
    rule_version: int
    severity: str
    tags: list[str]
    source_path: str
    member_path: str
    file_name: str
    line_no: int
    raw_line: str
    raw_truncated: bool
    fields: dict[str, Any]
    numeric_value: Decimal | None


@dataclass(frozen=True)
class TaskResult:
    status: str
    processed_lines: int
    matched_rows: int
    error: str | None = None


class TaskRepository(Protocol):
    def heartbeat(
        self, task_id: int, attempts: int, processed_lines: int, matched_rows: int
    ) -> bool: ...

    def complete(
        self,
        task_id: int,
        attempts: int,
        status: str,
        processed_lines: int,
        matched_rows: int,
        error: str | None = None,
    ) -> None: ...


class MatchWriter(Protocol):
    def delete_for_task(self, task_id: int) -> None: ...

    def write_batch(self, matches: Sequence[MatchRecord]) -> None: ...
```

`service/src/application/worker/processor.py`:

```python
import shutil
from pathlib import Path

from src.application.worker.ports import (
    ClaimedTask,
    MatchRecord,
    MatchWriter,
    TaskRepository,
    TaskResult,
)
from src.domain.archive.reader import iter_archive_lines
from src.domain.rules.evaluator import CompiledRuleSet, evaluate_line

RAW_LINE_LIMIT = 4096


class LeaseLost(RuntimeError):
    pass


class TaskProcessor:
    def __init__(
        self,
        repo: TaskRepository,
        writer: MatchWriter,
        rules: CompiledRuleSet,
        work_dir: Path,
        batch_size: int = 1000,
        heartbeat_every_lines: int = 20_000,
    ) -> None:
        self._repo = repo
        self._writer = writer
        self._rules = rules
        self._work_dir = Path(work_dir)
        self._batch_size = batch_size
        self._heartbeat_every_lines = heartbeat_every_lines

    def process(self, task: ClaimedTask) -> TaskResult:
        if task.attempts > 1:
            self._writer.delete_for_task(task.id)

        self._work_dir.mkdir(parents=True, exist_ok=True)
        local_path = self._copy_to_local(task)
        processed_lines = 0
        matched_rows = 0
        buffer: list[MatchRecord] = []

        try:
            for line in iter_archive_lines(
                local_path,
                encoding=task.encoding,
                fallback_encoding=task.fallback_encoding,
            ):
                processed_lines += 1
                for match in evaluate_line(self._rules, line.text, line.line_no):
                    buffer.append(
                        MatchRecord(
                            job_id=task.job_id,
                            file_task_id=task.id,
                            rule_id=match.rule_id,
                            rule_set_id=task.rule_set_id,
                            rule_version=task.rule_version,
                            severity=match.severity,
                            tags=match.tags,
                            source_path=str(task.source_path),
                            member_path=line.member_path,
                            file_name=line.file_name,
                            line_no=line.line_no,
                            raw_line=line.text[:RAW_LINE_LIMIT],
                            raw_truncated=line.truncated or len(line.text) > RAW_LINE_LIMIT,
                            fields=match.fields,
                            numeric_value=match.numeric_value,
                        )
                    )
                    matched_rows += 1

                if len(buffer) >= self._batch_size:
                    self._writer.write_batch(buffer)
                    buffer.clear()

                if processed_lines % self._heartbeat_every_lines == 0:
                    if not self._repo.heartbeat(task.id, task.attempts, processed_lines, matched_rows):
                        raise LeaseLost(f"lease lost for task {task.id}")

            if buffer:
                self._writer.write_batch(buffer)
            self._repo.complete(task.id, task.attempts, "succeeded", processed_lines, matched_rows)
            return TaskResult("succeeded", processed_lines, matched_rows)
        except LeaseLost:
            raise
        except Exception as exc:
            self._repo.complete(
                task.id,
                task.attempts,
                "failed",
                processed_lines,
                matched_rows,
                str(exc),
            )
            return TaskResult("failed", processed_lines, matched_rows, str(exc))
        finally:
            local_path.unlink(missing_ok=True)

    def _copy_to_local(self, task: ClaimedTask) -> Path:
        target = self._work_dir / f"{task.id}{task.source_path.suffix}"
        with task.source_path.open("rb") as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
        return target
```

`service/src/application/worker/__init__.py`:

```python
from src.application.worker.ports import ClaimedTask, MatchRecord, TaskResult
from src.application.worker.processor import TaskProcessor

__all__ = ["ClaimedTask", "MatchRecord", "TaskProcessor", "TaskResult"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd service && uv run --python 3.12 pytest tests/application/worker/test_processor.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add service/src/application/worker service/tests/application/worker
git commit -m "feat: 实现 Worker 归档处理与结果缓冲"
```

---

### Task 5: 七张核心表的 SQLModel 模型

**Files:**
- Create: `service/src/domain/models/__init__.py`
- Create: `service/src/domain/models/entities.py`
- Create: `service/src/infrastructure/database.py`
- Create: `service/tests/domain/models/test_entities.py`

**Interfaces:**
- Produces: SQLModel classes `DataSource`, `RuleSet`, `Job`, `FileTask`, `FileTaskAttempt`, `Match`, `Worker`
- Produces: `create_engine_from_settings()`, `create_db_and_tables(engine)`
- Consumes: none

- [ ] **Step 1: Write the failing test**

`service/tests/domain/models/test_entities.py`:

```python
from datetime import datetime, timezone

from sqlmodel import Session, SQLModel, create_engine, select

from src.domain.models.entities import (
    DataSource,
    FileTask,
    FileTaskAttempt,
    Job,
    Match,
    RuleSet,
    Worker,
)


def test_core_tables_round_trip() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        source = DataSource(name="nas", root_path="/data/logs")
        rules = RuleSet(name="baseline", version=1, config={"rules": []})
        session.add(source)
        session.add(rules)
        session.commit()
        session.refresh(source)
        session.refresh(rules)

        job = Job(
            name="batch-1",
            data_source_id=source.id,
            source_path="/data/logs/2026-10-24",
            rule_set_id=rules.id,
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        task = FileTask(
            job_id=job.id,
            source_path="/data/logs/2026-10-24/a.zip",
            file_name="a.zip",
            size_bytes=10,
            mtime=datetime.now(timezone.utc),
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        attempt = FileTaskAttempt(
            file_task_id=task.id,
            attempt_no=1,
            worker_id="w1",
            worker_version="0.1.0",
            status="succeeded",
        )
        match = Match(
            job_id=job.id,
            file_task_id=task.id,
            rule_id="error",
            rule_set_id=rules.id,
            rule_version=1,
            source_path=task.source_path,
            member_path="app.log",
            file_name="app.log",
            line_no=1,
            raw_line="ERROR",
            fields={"level": "ERROR"},
        )
        worker = Worker(id="w1", hostname="host1", version="0.1.0")
        session.add(attempt)
        session.add(match)
        session.add(worker)
        session.commit()

        assert session.exec(select(Match)).one().fields == {"level": "ERROR"}
        assert session.exec(select(FileTaskAttempt)).one().attempt_no == 1
        assert session.exec(select(Worker)).one().version == "0.1.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd service && uv run --python 3.12 pytest tests/domain/models/test_entities.py -v`

Expected: FAIL because models do not exist.

- [ ] **Step 3: Write minimal implementation**

`service/src/domain/models/entities.py`:

```python
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import Column, JSON, Numeric
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DataSource(SQLModel, table=True):
    __tablename__ = "data_sources"
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    type: str = "mount"
    root_path: str
    read_only: bool = True
    copy_to_local: bool = True
    enabled: bool = True
    description: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class RuleSet(SQLModel, table=True):
    __tablename__ = "rule_sets"
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    version: int
    config: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_by: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Job(SQLModel, table=True):
    __tablename__ = "jobs"
    id: int | None = Field(default=None, primary_key=True)
    name: str
    data_source_id: int = Field(foreign_key="data_sources.id")
    source_path: str
    file_filter: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    rule_set_id: int = Field(foreign_key="rule_sets.id")
    status: str = "pending"
    total_files: int = 0
    total_bytes: int = 0
    cancel_requested: bool = False
    created_by: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class FileTask(SQLModel, table=True):
    __tablename__ = "file_tasks"
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    source_path: str
    file_name: str
    size_bytes: int
    mtime: datetime | None = None
    status: str = "pending"
    attempts: int = 0
    max_attempts: int = 5
    scheduled_at: datetime = Field(default_factory=utcnow)
    worker_id: str | None = None
    heartbeat_at: datetime | None = None
    processed_lines: int = 0
    matched_rows: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class FileTaskAttempt(SQLModel, table=True):
    __tablename__ = "file_task_attempts"
    file_task_id: int = Field(foreign_key="file_tasks.id", primary_key=True)
    attempt_no: int = Field(primary_key=True)
    worker_id: str
    worker_version: str
    status: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    processed_lines: int = 0
    matched_rows: int = 0
    error: str | None = None


class Match(SQLModel, table=True):
    __tablename__ = "matches"
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    file_task_id: int = Field(index=True)
    rule_id: str
    rule_set_id: int
    rule_version: int
    source_path: str
    member_path: str
    file_name: str
    line_no: int
    raw_line: str
    raw_truncated: bool = False
    fields: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    numeric_value: Decimal | None = Field(
        default=None,
        sa_column=Column(Numeric(38, 10), nullable=True),
    )
    severity: str = "info"
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=utcnow)


class Worker(SQLModel, table=True):
    __tablename__ = "workers"
    id: str = Field(primary_key=True)
    hostname: str
    version: str
    status: str = "online"
    drain_requested: bool = False
    current_task_id: int | None = None
    max_concurrent_copies: int = 4
    cpu_percent: float | None = None
    memory_bytes: int | None = None
    temp_disk_free_bytes: int | None = None
    last_heartbeat: datetime = Field(default_factory=utcnow)
    started_at: datetime = Field(default_factory=utcnow)
    last_error: str | None = None
```

`service/src/domain/models/__init__.py`:

```python
from src.domain.models.entities import (
    DataSource,
    FileTask,
    FileTaskAttempt,
    Job,
    Match,
    RuleSet,
    Worker,
)

__all__ = [
    "DataSource",
    "FileTask",
    "FileTaskAttempt",
    "Job",
    "Match",
    "RuleSet",
    "Worker",
]
```

`service/src/infrastructure/database.py`:

```python
from sqlmodel import SQLModel, create_engine

from src.config.settings import settings

engine = create_engine(settings.database_url, echo=False)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd service && uv run --python 3.12 pytest tests/domain/models/test_entities.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add service/src/domain/models service/src/infrastructure/database.py service/tests/domain/models
git commit -m "feat: 建立七张核心表的 SQLModel 模型"
```

---

### Task 6: 集成验证与开发文档

**Files:**
- Create: `service/README.md`
- Create: `.env.example`
- Modify: `README.md`

**Interfaces:**
- Consumes: all prior tasks
- Produces: runnable dev commands and documented M0 scope

- [ ] **Step 1: Run the full test suite**

```bash
cd service
uv sync --python 3.12
uv run --python 3.12 pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Run static checks**

```bash
cd service
uv run --python 3.12 ruff check src tests
```

Expected: no errors. Fix imports/formatting if ruff reports issues; do not change behavior.

- [ ] **Step 3: Add dev documentation**

`service/README.md`:

```markdown
# LogHarvest Service

M0 核心引擎：规则匹配、压缩包流式读取、Worker 处理器和核心数据模型。

## 开发

```bash
uv sync --python 3.12
uv run --python 3.12 pytest -q
uv run --python 3.12 uvicorn src.main:app --reload --port 8000
```

健康检查：`GET /api/health`。

## M0 范围

- 规则编译/匹配：关键字、正则、数值门限。
- 压缩包发现与流式读取：zip、tar.gz、单文件 gz/bz2/xz、原始文本。
- Worker 处理器：本地复制、逐行匹配、批量结果缓冲、重试清场。
- 七张核心表的 SQLModel 模型。
- 不包含：PostgreSQL 队列适配、任务 API、前端、Worker 管理页。
```

`.env.example`:

```dotenv
LOGHARVEST_ENVIRONMENT=dev
LOGHARVEST_DATABASE_URL=sqlite:///./logharvest.db
LOGHARVEST_AUTO_CREATE_TABLES=true
LOGHARVEST_WORKER_VERSION=0.1.0
LOGHARVEST_MAX_CONCURRENT_COPIES=4
```

Update root `README.md` "仓库状态" section to say M0 core engine is in progress and link `service/README.md`.

- [ ] **Step 4: Commit**

```bash
git add service/README.md .env.example README.md
git commit -m "docs: 补充 M0 开发说明与环境配置"
```

---

## Plan Self-Review

- 规则引擎覆盖关键字 any/all、正则命名分组、数值门限和禁用规则。
- 归档读取覆盖递归发现、zip、tar.gz、单文件 gz、原始文本、GBK 回退、超长行截断。
- Worker 处理器覆盖读取、匹配、批量写入、重试清场、成功/失败终态。
- SQLModel 覆盖 7 张核心表，使用 SQLite 测试；PostgreSQL 队列适配留到 M1。
- 未覆盖：FastAPI 业务 API、前端、PG `SKIP LOCKED`、Worker 管理/升级、Alembic；这些在 M1 计划中实现。
