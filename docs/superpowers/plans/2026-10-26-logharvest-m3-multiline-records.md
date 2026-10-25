# LogHarvest M3 多行记录 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 规则集可选声明 `record_start_regex`，Worker 按成员流式组装多行记录并只对整条记录评估一次，同时用行数/字节上限、起始正则超时与结构化日志约束最坏耗时和内存。

**Architecture:** 领域层只扩规则契约（`RuleSetConfig.record_start_regex` → `CompiledRuleSet.record_start`）；新增无数据库/日志依赖的 `RecordAssembler` 做流式状态机；`TaskProcessor` 保留现有单行路径原样，新增多行路径复用批次写入、租约栅栏、心跳与成员级 `partial` 汇总。前端只加一个表单字段并保留 `raw_line` 换行展示。

**Tech Stack:** Python 3.12、pydantic v2、`regex`、FastAPI、SQLModel、pytest（SQLite）、真实 PostgreSQL 集成、Vue 3 + Element Plus。

**Spec:** [M3 多行记录规则设计](../specs/2026-10-26-logharvest-m3-multiline-records-design.md)

## Global Constraints

- 命令一律 `cd service && uv run ...`；前端 `cd web && pnpm ...`。
- 提交信息中文前缀 `feat:` / `fix:` / `docs:` / `chore:`；每个 Task 一个提交。
- **单行模式行为与性能不变**：`record_start_regex` 为空时走现有逐行评估代码路径。
- **不新增数据库列、不新增 Alembic 迁移、不改 `matches` API 形状**；规则配置本来就是 JSON。
- 记录绝不跨成员；成员边界按 `line.member_path` 变化识别，并用 `line.line_no == 1` 兜住同名成员（reader 对每个成员从 1 重新计数）。
- 记录上限：`LOGHARVEST_RECORD_MAX_LINES` 默认 `2000`、`LOGHARVEST_RECORD_MAX_BYTES` 默认 `1048576`，均必须 `> 0`。
- 起始正则 `search` 超时 50 ms，超时视为永久失败（`retryable=False`）。
- 组装器不得依赖数据库或日志；可观测性由 processor 输出结构化事件。
- 取消/丢租约路径不 flush 未完成记录，保持现有语义。
- 不引入 JSON/Monaco 编辑器；前端本机内置浏览器是隐藏页，只以 `pnpm typecheck` + `pnpm build` 和接口契约为准，**不得声称界面视觉正确**。
- 发布顺序强制：先升级全部 Worker，再创建/保存多行规则集，最后创建作业。
- 当前基线：SQLite `336 passed / 16 skipped`、PG 集成 `15 passed`、ruff/mypy 干净；M3 完成后记录实际数字。

---

### Task 1: 规则契约与编译

**Files:**
- Modify: `service/src/domain/rules/models.py:55-67`
- Modify: `service/src/domain/rules/evaluator.py:38-76`
- Test: `service/tests/domain/rules/test_evaluator.py`

**Interfaces:**
- Consumes: 现有 `RuleSetConfig`、`compile_ruleset`。
- Produces:
  - `RuleSetConfig.record_start_regex: str | None = None`（纯空白 → `ValidationError`）
  - `CompiledRuleSet.record_start: regex.Pattern | None`（`None` 即单行模式）
  - 非法正则 → `ValueError("invalid record_start_regex: ...")`

- [ ] **Step 1: 写失败测试**

在 `service/tests/domain/rules/test_evaluator.py` 末尾追加：

```python
def test_record_start_regex_compiles_to_pattern() -> None:
    compiled = compile_ruleset(
        RuleSetConfig(name="t", record_start_regex=r"^\d{4}-\d{2}-\d{2} ", rules=[])
    )
    assert compiled.record_start is not None
    assert compiled.record_start.search("2026-10-26 01:00:00 ERROR")


def test_absent_record_start_regex_keeps_single_line_mode() -> None:
    compiled = compile_ruleset(RuleSetConfig(name="t", rules=[]))
    assert compiled.record_start is None


def test_blank_record_start_regex_is_rejected() -> None:
    with pytest.raises(ValidationError, match="record_start_regex"):
        RuleSetConfig(name="t", record_start_regex="   ", rules=[])


def test_invalid_record_start_regex_is_rejected() -> None:
    with pytest.raises(ValueError, match="record_start_regex"):
        compile_ruleset(RuleSetConfig(name="t", record_start_regex="(", rules=[]))


def test_keyword_all_matches_across_record_lines() -> None:
    compiled = compile_one(
        Rule(id="oom", match=KeywordMatch(all=["OutOfMemoryError", "com.example"]))
    )
    text = "2026-10-26 ERROR java.lang.OutOfMemoryError\n\tat com.example.Service.run(Service.java:1)"
    assert len(evaluate_line(compiled, text, 10)) == 1


def test_regex_without_m_flag_anchors_to_record_boundaries_only() -> None:
    compiled = compile_one(
        Rule(id="anchor", match=RegexMatch(pattern=r"^at com\.example"))
    )
    text = "ERROR boom\nat com.example.A.run(A.java:1)"
    assert evaluate_line(compiled, text, 1) == []


def test_regex_with_m_flag_matches_continuation_line() -> None:
    compiled = compile_one(
        Rule(id="anchor", match=RegexMatch(pattern=r"^at com\.example", flags="m"))
    )
    text = "ERROR boom\nat com.example.A.run(A.java:1)"
    assert len(evaluate_line(compiled, text, 1)) == 1


def test_threshold_field_can_come_from_continuation_line() -> None:
    compiled = compile_one(
        Rule(
            id="slow",
            match=RegexMatch(pattern=r"elapsed=(?P<elapsed_ms>\d+)ms"),
            threshold=Threshold(field="elapsed_ms", op="gt", value=Decimal(5000)),
        )
    )
    text = "WARN request failed\nelapsed=6000ms"
    matches = evaluate_line(compiled, text, 5)
    assert matches[0].numeric_value == Decimal(6000)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd service && uv run pytest -q tests/domain/rules/test_evaluator.py -k "record_start or across_record or continuation or m_flag or without_m_flag"`
Expected: FAIL（`record_start_regex` 不是合法字段 / `record_start` 属性不存在）

- [ ] **Step 3: 最小实现**

`models.py` 的 `RuleSetConfig` 增加字段与校验（放在 `fallback_encoding` 之后、`rules` 之前）：

```python
    record_start_regex: str | None = None
```

```python
    @model_validator(mode="after")
    def validate_record_start_regex(self) -> "RuleSetConfig":
        if self.record_start_regex is not None and not self.record_start_regex.strip():
            raise ValueError("record_start_regex must be null or a non-blank regex")
        return self
```

`evaluator.py` 的 `CompiledRuleSet` 增加默认字段，并在 `compile_ruleset` 中编译：

```python
@dataclass(frozen=True)
class CompiledRuleSet:
    config: RuleSetConfig
    rules: tuple[CompiledRule, ...]
    record_start: regex.Pattern | None = None
```

```python
def compile_ruleset(config: RuleSetConfig) -> CompiledRuleSet:
    record_start: regex.Pattern | None = None
    if config.record_start_regex is not None:
        try:
            record_start = regex.compile(config.record_start_regex)
        except regex.error as exc:
            raise ValueError(f"invalid record_start_regex: {exc}") from exc
    compiled: list[CompiledRule] = []
    # ...现有循环原样保留...
    return CompiledRuleSet(config=config, rules=tuple(compiled), record_start=record_start)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd service && uv run pytest -q tests/domain/rules/test_evaluator.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add service/src/domain/rules/models.py service/src/domain/rules/evaluator.py service/tests/domain/rules/test_evaluator.py
git commit -m "feat: 规则集支持可选的记录起始正则"
```

---

### Task 2: RecordAssembler 流式记录组装器

**Files:**
- Create: `service/src/application/worker/records.py`
- Test: `service/tests/application/worker/test_records.py`

**Interfaces:**
- Consumes: `src.domain.archive.models.LineRecord`（字段 `line_no`、`text`、`member_path`、`file_name`、`truncated`）。
- Produces:
  - `LogRecord(start_line, end_line, text, member_path, file_name, truncated, oversize_reason=None)`
  - `RecordStartError(member_path: str, line_no: int, timeout_ms: int)`
  - `RecordAssembler(start_pattern, *, max_lines, max_bytes, start_timeout_ms=50)`
    - `feed(line: LineRecord) -> list[LogRecord]`
    - `flush() -> list[LogRecord]`
    - `reset_member() -> None`（未 flush 就调用 → `RuntimeError`）
    - `start_matches: int`、`preamble_lines: int`

- [ ] **Step 1: 写失败测试**

创建 `service/tests/application/worker/test_records.py`：

```python
import regex
import pytest

from src.application.worker.records import (
    RecordAssembler,
    RecordStartError,
)
from src.domain.archive.models import LineRecord

START = regex.compile(r"^\d{4}-\d{2}-\d{2} ")


def line(no: int, text: str, member: str = "app.log", truncated: bool = False) -> LineRecord:
    return LineRecord(
        line_no=no,
        text=text,
        member_path=member,
        file_name=member.rsplit("/", 1)[-1],
        truncated=truncated,
    )


def make_assembler(**overrides) -> RecordAssembler:
    options = {"max_lines": 100, "max_bytes": 10_000}
    options.update(overrides)
    return RecordAssembler(START, **options)


def test_start_line_opens_record_and_next_start_flushes() -> None:
    assembler = make_assembler()
    assert assembler.feed(line(1, "preamble")) == []
    assert assembler.feed(line(2, "2026-10-26 01:00:01 INFO ok")) == []
    assert assembler.feed(line(3, "2026-10-26 01:00:02 ERROR boom")) == []
    emitted = assembler.feed(line(4, "2026-10-26 01:00:03 INFO done"))

    assert len(emitted) == 1
    record = emitted[0]
    assert record.start_line == 3
    assert record.end_line == 3
    assert record.text == "2026-10-26 01:00:02 ERROR boom"
    assert record.member_path == "app.log"
    assert record.file_name == "app.log"
    assert record.truncated is False
    assert record.oversize_reason is None
    assert assembler.preamble_lines == 1
    assert assembler.start_matches == 3


def test_continuation_lines_join_with_newline_and_keep_empty_lines() -> None:
    assembler = make_assembler()
    assembler.feed(line(1, "2026-10-26 01:00:01 ERROR boom"))
    assert assembler.feed(line(2, "")) == []
    assert assembler.feed(line(3, "\tat com.example.A.run")) == []

    [record] = assembler.flush()
    assert record.start_line == 1
    assert record.end_line == 3
    assert record.text == "2026-10-26 01:00:01 ERROR boom\n\n\tat com.example.A.run"


def test_preamble_is_ignored_and_never_evaluated() -> None:
    assembler = make_assembler()
    assembler.feed(line(1, "ERROR before any start"))
    assembler.feed(line(2, "still preamble"))

    assert assembler.flush() == []
    assert assembler.preamble_lines == 2
    assert assembler.start_matches == 0


def test_flush_at_eof_and_member_reset() -> None:
    assembler = make_assembler()
    assembler.feed(line(1, "2026-10-26 01:00:01 ERROR boom"))
    [record] = assembler.flush()
    assert record.text.endswith("ERROR boom")

    assembler.reset_member()
    assert assembler.start_matches == 0
    assert assembler.preamble_lines == 0

    assembler.feed(line(1, "2026-10-26 02:00:00 ERROR other", member="b.log"))
    [second] = assembler.flush()
    assert second.member_path == "b.log"
    assert second.start_line == 1


def test_reset_member_requires_flush_first() -> None:
    assembler = make_assembler()
    assembler.feed(line(1, "2026-10-26 01:00:01 ERROR boom"))

    with pytest.raises(RuntimeError, match="flush"):
        assembler.reset_member()


def test_max_lines_flushes_truncated_and_skips_until_next_start() -> None:
    assembler = make_assembler(max_lines=2)
    assembler.feed(line(1, "2026-10-26 01:00:01 ERROR one"))
    assert assembler.feed(line(2, "continuation")) == []
    [record] = assembler.feed(line(3, "OVERFLOW skipped"))

    assert record.start_line == 1
    assert record.end_line == 2
    assert record.text == "2026-10-26 01:00:01 ERROR one\ncontinuation"
    assert record.truncated is True
    assert record.oversize_reason == "max_lines"

    assert assembler.feed(line(4, "also skipped")) == []
    assert assembler.feed(line(5, "2026-10-26 01:00:05 ERROR two")) == []
    [next_record] = assembler.flush()
    assert next_record.start_line == 5
    assert next_record.truncated is False


def test_max_bytes_flushes_truncated_and_skips_triggering_line() -> None:
    assembler = make_assembler(max_bytes=30)
    assembler.feed(line(1, "2026-10-26 ERROR"))  # 16 bytes
    assembler.feed(line(2, "1234567890"))  # 17 more, still under 30
    [record] = assembler.feed(line(3, "this line pushes over the limit"))

    assert record.end_line == 2
    assert record.oversize_reason == "max_bytes"
    assert "this line" not in record.text

    assert assembler.feed(line(4, "2026-10-26 next start opens directly")) == []
    [next_record] = assembler.flush()
    assert next_record.start_line == 4


def test_oversized_start_line_is_emitted_once_and_enters_skip_state() -> None:
    assembler = RecordAssembler(
        regex.compile(r"^S"), max_lines=10, max_bytes=10
    )
    [record] = assembler.feed(line(1, "S too long for limit"))

    assert record.start_line == 1
    assert record.end_line == 1
    assert record.truncated is True
    assert record.oversize_reason == "max_bytes"
    assert assembler.feed(line(2, "skipped")) == []

    assert assembler.feed(line(3, "S ok")) == []
    [next_record] = assembler.flush()
    assert next_record.start_line == 3


def test_reader_truncated_line_marks_record_truncated() -> None:
    assembler = make_assembler()
    assembler.feed(line(1, "2026-10-26 ERROR boom", truncated=True))
    [record] = assembler.flush()

    assert record.truncated is True
    assert record.oversize_reason is None


def test_unicode_bytes_count_uses_utf8() -> None:
    text = "2026-10-26 中文"
    assembler = make_assembler(max_bytes=len(text.encode("utf-8")))
    assembler.feed(line(1, text))
    [record] = assembler.feed(line(2, "续行"))

    assert record.end_line == 2
    assert record.oversize_reason == "max_bytes"
    assert record.text == text


class TimeoutPattern:
    def search(self, text: str, timeout: float):
        raise TimeoutError


def test_start_regex_timeout_raises_record_start_error() -> None:
    assembler = RecordAssembler(
        TimeoutPattern(), max_lines=10, max_bytes=100, start_timeout_ms=50
    )

    with pytest.raises(RecordStartError) as caught:
        assembler.feed(line(7, "anything", member="nested/app.log"))

    assert caught.value.member_path == "nested/app.log"
    assert caught.value.line_no == 7
    assert caught.value.timeout_ms == 50
    assert "nested/app.log" in str(caught.value)
    assert "7" in str(caught.value)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd service && uv run pytest -q tests/application/worker/test_records.py`
Expected: FAIL（`ModuleNotFoundError: src.application.worker.records`）

- [ ] **Step 3: 实现 `records.py`**

```python
from dataclasses import dataclass, field

import regex

from src.domain.archive.models import LineRecord


class RecordStartError(RuntimeError):
    def __init__(self, member_path: str, line_no: int, timeout_ms: int) -> None:
        super().__init__(
            f"record start regex timed out after {timeout_ms} ms at {member_path}:{line_no}"
        )
        self.member_path = member_path
        self.line_no = line_no
        self.timeout_ms = timeout_ms


@dataclass(frozen=True)
class LogRecord:
    start_line: int
    end_line: int
    text: str
    member_path: str
    file_name: str
    truncated: bool
    oversize_reason: str | None = None


@dataclass
class _Accumulator:
    start_line: int
    end_line: int
    member_path: str
    file_name: str
    parts: list[str] = field(default_factory=list)
    byte_count: int = 0
    truncated: bool = False


class RecordAssembler:
    def __init__(
        self,
        start_pattern: regex.Pattern,
        *,
        max_lines: int,
        max_bytes: int,
        start_timeout_ms: int = 50,
    ) -> None:
        if max_lines < 1:
            raise ValueError("max_lines must be >= 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        if start_timeout_ms < 1:
            raise ValueError("start_timeout_ms must be >= 1")
        self._start = start_pattern
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._start_timeout = start_timeout_ms / 1000
        self._start_timeout_ms = start_timeout_ms
        self._start_matches = 0
        self._preamble_lines = 0
        self._current: _Accumulator | None = None
        self._skipping = False

    @property
    def start_matches(self) -> int:
        return self._start_matches

    @property
    def preamble_lines(self) -> int:
        return self._preamble_lines

    def reset_member(self) -> None:
        if self._current is not None:
            raise RuntimeError("flush() must be called before reset_member()")
        self._start_matches = 0
        self._preamble_lines = 0
        self._skipping = False

    def _is_start(self, line: LineRecord) -> bool:
        try:
            return self._start.search(line.text, timeout=self._start_timeout) is not None
        except TimeoutError as exc:
            raise RecordStartError(
                line.member_path, line.line_no, self._start_timeout_ms
            ) from exc

    def _open(self, line: LineRecord) -> list[LogRecord]:
        text_bytes = len(line.text.encode("utf-8"))
        self._current = _Accumulator(
            start_line=line.line_no,
            end_line=line.line_no,
            member_path=line.member_path,
            file_name=line.file_name,
            parts=[line.text],
            byte_count=text_bytes,
            truncated=line.truncated,
        )
        if text_bytes > self._max_bytes:
            self._skipping = True
            return [self._finish(truncated=True, reason="max_bytes")]
        return []

    def _append(self, line: LineRecord) -> list[LogRecord]:
        assert self._current is not None
        extra = 1 + len(line.text.encode("utf-8"))
        reason: str | None = None
        if len(self._current.parts) >= self._max_lines:
            reason = "max_lines"
        elif self._current.byte_count + extra > self._max_bytes:
            reason = "max_bytes"
        if reason is not None:
            self._skipping = True
            return [self._finish(truncated=True, reason=reason)]

        self._current.parts.append(line.text)
        self._current.end_line = line.line_no
        self._current.byte_count += extra
        self._current.truncated = self._current.truncated or line.truncated
        return []

    def _finish(self, *, truncated: bool = False, reason: str | None = None) -> LogRecord:
        current = self._current
        assert current is not None
        record = LogRecord(
            start_line=current.start_line,
            end_line=current.end_line,
            text="\n".join(current.parts),
            member_path=current.member_path,
            file_name=current.file_name,
            truncated=current.truncated or truncated,
            oversize_reason=reason,
        )
        self._current = None
        return record

    def feed(self, line: LineRecord) -> list[LogRecord]:
        if self._skipping:
            if not self._is_start(line):
                return []
            self._skipping = False
            self._start_matches += 1
            return self._open(line)

        if self._current is None:
            if not self._is_start(line):
                self._preamble_lines += 1
                return []
            self._start_matches += 1
            return self._open(line)

        if self._is_start(line):
            completed = self._finish()
            self._start_matches += 1
            return [completed, *self._open(line)]

        return self._append(line)

    def flush(self) -> list[LogRecord]:
        self._skipping = False
        if self._current is None:
            return []
        return [self._finish()]
```

- [ ] **Step 4: 运行确认通过**

Run: `cd service && uv run pytest -q tests/application/worker/test_records.py && uv run ruff check src/application/worker/records.py tests/application/worker/test_records.py && uv run mypy`
Expected: PASS、ruff/mypy 干净

- [ ] **Step 5: 提交**

```bash
git add service/src/application/worker/records.py service/tests/application/worker/test_records.py docs/superpowers/specs/2026-10-26-logharvest-m3-multiline-records-design.md
git commit -m "feat: 新增流式多行记录组装器"
```

---

### Task 3: Processor 多行路径、Settings 与 Worker 接线

**Files:**
- Modify: `service/src/application/worker/processor.py:1-301`
- Modify: `service/src/config/settings.py:14-29`
- Modify: `service/src/worker/main.py:35-90, 250-275`
- Test: `service/tests/application/worker/test_processor.py`
- Test: `service/tests/worker/test_runtime.py`

**Interfaces:**
- Consumes: Task 2 的 `RecordAssembler` / `RecordStartError` / `LogRecord`；Task 1 的 `CompiledRuleSet.record_start`。
- Produces:
  - `TaskProcessor(..., record_max_lines: int = 2000, record_max_bytes: int = 1_048_576, logger=None)`
  - `WorkerRuntime(..., record_max_lines=2000, record_max_bytes=1_048_576)` 并把值 + logger 传给 `TaskProcessor`
  - `Settings.record_max_lines: int = Field(default=2000, gt=0)`、`Settings.record_max_bytes: int = Field(default=1_048_576, gt=0)`
  - 结构化事件：`record_oversized`（warning）、`record_start_not_found`（warning）

- [ ] **Step 1: 写失败测试**

在 `service/tests/application/worker/test_processor.py` 追加（顶部补 `from src.domain.archive.models import LineRecord`）：

```python
class ListLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def warning(self, event: str, **kwargs) -> None:
        self.events.append(("warning", event, kwargs))

    def error(self, event: str, **kwargs) -> None:
        self.events.append(("error", event, kwargs))

    def info(self, event: str, **kwargs) -> None:
        self.events.append(("info", event, kwargs))


def make_multiline_rules(**overrides):
    options = {
        "record_start_regex": r"^\d{4}-\d{2}-\d{2} ",
        "rules": [Rule(id="error", match=KeywordMatch(any=["ERROR"]), severity="error")],
    }
    options.update(overrides)
    return compile_ruleset(RuleSetConfig(name="m", **options))


def test_multiline_record_is_evaluated_once_with_start_line_anchor(tmp_path: Path) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    logger = ListLogger()
    processor = make_processor(
        repo,
        writer,
        tmp_path / "work",
        rules=make_multiline_rules(),
        logger=logger,
    )
    task = make_task(
        tmp_path,
        members={
            "app.log": (
                "boot\n"
                "2026-10-26 01:00:01 INFO ok\n"
                "2026-10-26 01:00:02 ERROR boom\n"
                "\tat com.example.A.run(A.java:1)\n"
            )
        },
    )

    result = processor.process(task)

    assert result.status == "succeeded"
    assert result.processed_lines == 4
    assert result.matched_rows == 1
    [match] = writer.written
    assert match.line_no == 3
    assert match.raw_truncated is False
    assert match.raw_line == (
        "2026-10-26 01:00:02 ERROR boom\n\tat com.example.A.run(A.java:1)"
    )
    assert repo.completed == [(10, 1, "succeeded", 4, 1, None)]


def test_multiline_records_do_not_cross_members_and_report_missing_start(
    tmp_path: Path,
) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    logger = ListLogger()
    processor = make_processor(
        repo,
        writer,
        tmp_path / "work",
        rules=make_multiline_rules(),
        logger=logger,
    )
    task = make_task(
        tmp_path,
        members={
            "a.log": "2026-10-26 ERROR first\n",
            "b.log": "no start marker ERROR here\n",
        },
    )

    result = processor.process(task)

    assert result.status == "succeeded"
    assert [(m.member_path, m.line_no) for m in writer.written] == [("a.log", 1)]
    warnings = [event for _, event, _ in logger.events if event == "record_start_not_found"]
    assert warnings == ["record_start_not_found"]
    not_found = next(kwargs for _, event, kwargs in logger.events if event == "record_start_not_found")
    assert not_found == {"member_path": "b.log", "lines": 1}


def test_multiline_oversized_record_is_truncated_and_skips_to_next_start(
    tmp_path: Path,
) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    logger = ListLogger()
    processor = make_processor(
        repo,
        writer,
        tmp_path / "work",
        rules=make_multiline_rules(),
        record_max_lines=2,
        logger=logger,
    )
    task = make_task(
        tmp_path,
        members={
            "app.log": (
                "2026-10-26 ERROR keep\n"
                "continuation\n"
                "ERROR skipped overflow\n"
                "2026-10-26 ERROR second\n"
            )
        },
    )

    result = processor.process(task)

    assert result.status == "succeeded"
    assert [m.line_no for m in writer.written] == [1, 4]
    assert writer.written[0].raw_truncated is True
    assert writer.written[1].raw_truncated is False
    oversized = [kwargs for _, event, kwargs in logger.events if event == "record_oversized"]
    assert oversized == [
        {
            "member_path": "app.log",
            "start_line": 1,
            "lines": 2,
            "bytes": len("2026-10-26 ERROR keep\ncontinuation".encode("utf-8")),
            "reason": "max_lines",
        }
    ]


def test_multiline_start_regex_timeout_fails_task_permanently(
    tmp_path: Path, monkeypatch
) -> None:
    from src.application.worker.records import RecordStartError

    class TimingOutAssembler:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def feed(self, line):
            raise RecordStartError(line.member_path, line.line_no, 50)

        def flush(self):
            return []

        def reset_member(self) -> None:
            pass

        start_matches = 0
        preamble_lines = 0

    monkeypatch.setattr(processor_module, "RecordAssembler", TimingOutAssembler)
    repo = FakeRepo()
    writer = FakeWriter()
    task = make_task(tmp_path, members={"app.log": "anything\n"})

    result = make_processor(repo, writer, tmp_path / "work", rules=make_multiline_rules()).process(task)

    assert result.status == "failed"
    assert "app.log" in (result.error or "")
    assert "50" in (result.error or "")
    assert repo.retryables == [(10, False)]
    assert writer.written == []


def test_damaged_member_flushes_accumulated_record_before_partial(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeScan:
        def __init__(self) -> None:
            self.member_errors = [
                ArchiveMemberError("a.log", "corrupt", scan_incomplete=True)
            ]
            self.succeeded_members = {"b.log"}
            self.lines = iter(
                [LineRecord(1, "2026-10-26 ERROR boom", "a.log", "a.log")]
            )

    monkeypatch.setattr(processor_module, "scan_archive", lambda *args, **kwargs: FakeScan())
    repo = FakeRepo()
    writer = FakeWriter()
    task = make_task(tmp_path, members={"a.log": "2026-10-26 ERROR boom\n"})

    result = make_processor(repo, writer, tmp_path / "work", rules=make_multiline_rules()).process(task)

    assert result.status == "partial"
    assert [(m.member_path, m.line_no) for m in writer.written] == [("a.log", 1)]
    assert repo.retryables == [(10, False)]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"record_max_lines": 0}, "record_max_lines"),
        ({"record_max_bytes": 0}, "record_max_bytes"),
    ],
)
def test_processor_rejects_invalid_record_limits(
    tmp_path: Path, overrides: dict, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        make_processor(FakeRepo(), FakeWriter(), tmp_path / "work", **overrides)
```

在 `service/tests/worker/test_runtime.py` 追加：

```python
def test_run_once_forwards_record_limits_and_logger(tmp_path, monkeypatch):
    captured = {}

    class RecordingProcessor:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def process(self, task):
            return TaskResult("succeeded", 0, 0)

    monkeypatch.setattr(worker_main, "TaskProcessor", RecordingProcessor)
    logger = object()
    runtime = make_runtime(
        FakeRepo([make_task(tmp_path)]),
        FakeWriter(),
        tmp_path,
        record_max_lines=11,
        record_max_bytes=22,
        logger=logger,
    )

    assert runtime.run_once() is True
    assert captured["record_max_lines"] == 11
    assert captured["record_max_bytes"] == 22
    assert captured["logger"] is logger
```

- [ ] **Step 2: 运行确认失败**

Run: `cd service && uv run pytest -q tests/application/worker/test_processor.py tests/worker/test_runtime.py`
Expected: FAIL（`TaskProcessor` 不接受 `record_max_lines`；多行模式仍按单行评估）

- [ ] **Step 3: 实现**

`settings.py` 在 `matches_batch_size` 附近增加：

```python
    record_max_lines: int = Field(default=2000, gt=0)
    record_max_bytes: int = Field(default=1_048_576, gt=0)
```

（文件顶部补 `from pydantic import Field, model_validator`。）

`processor.py`：

1. 导入：

```python
from typing import Any, BinaryIO

from src.application.worker.records import LogRecord, RecordAssembler, RecordStartError
```

2. `TaskProcessor.__init__` 签名尾部（保持现有参数不动，只在 `nas_read_only_policy` 之后追加）：

```python
        nas_read_only_policy: str = POLICY_ENFORCE,
        record_max_lines: int = 2000,
        record_max_bytes: int = 1_048_576,
        logger: Any = None,
    ) -> None:
```

在现有校验之后追加：

```python
        if record_max_lines < 1:
            raise ValueError("record_max_lines must be >= 1")
        if record_max_bytes < 1:
            raise ValueError("record_max_bytes must be >= 1")
```

在现有赋值之后追加：

```python
        self._record_max_lines = record_max_lines
        self._record_max_bytes = record_max_bytes
        self._logger = logger
```

3. 增加辅助方法（放在 `_member_outcome` 之后）：

```python
    def _log(self, level: str, event: str, **kwargs: object) -> None:
        if self._logger is not None:
            getattr(self._logger, level)(event, **kwargs)

    def _tick(
        self,
        task: ClaimedTask,
        processed_lines: int,
        matched_rows: int,
        last_heartbeat: float,
    ) -> float:
        now = self._clock()
        if (
            processed_lines % self._heartbeat_every_lines == 0
            or now - last_heartbeat >= self._heartbeat_every_seconds
        ):
            if not self._repo.heartbeat(task.id, task.attempts, processed_lines, matched_rows):
                raise LeaseLost(f"lease lost for task {task.id}")
            if self._cancel_check is not None and self._cancel_check():
                raise JobCancelled(f"job {task.job_id} cancelled")
            if self._on_lease_refresh is not None:
                self._on_lease_refresh()
            return now
        return last_heartbeat

    def _enqueue(
        self,
        task: ClaimedTask,
        buffer: list[MatchRecord],
        *,
        text: str,
        line_no: int,
        member_path: str,
        file_name: str,
        truncated: bool,
    ) -> int:
        matches = evaluate_line(self._rules, text, line_no)
        if not matches:
            return 0
        raw_line, raw_truncated = _truncate_raw_line(text)
        raw_truncated = raw_truncated or truncated
        for match in matches:
            buffer.append(
                MatchRecord(
                    job_id=task.job_id,
                    file_task_id=task.id,
                    rule_id=match.rule_id,
                    rule_set_id=task.rule_set_id,
                    rule_version=task.rule_version,
                    severity=match.severity,
                    tags=list(match.tags),
                    source_path=str(task.source_path),
                    member_path=member_path,
                    file_name=file_name,
                    line_no=line_no,
                    raw_line=raw_line,
                    raw_truncated=raw_truncated,
                    fields=dict(match.fields),
                    numeric_value=match.numeric_value,
                )
            )
        return len(matches)

    def _flush_batch(self, task: ClaimedTask, buffer: list[MatchRecord]) -> None:
        if len(buffer) >= self._batch_size:
            self._verify_lease(task)
            self._writer.write_batch(tuple(buffer))
            buffer.clear()

    def _emit_record(
        self, task: ClaimedTask, buffer: list[MatchRecord], record: LogRecord
    ) -> int:
        if record.oversize_reason is not None:
            self._log(
                "warning",
                "record_oversized",
                member_path=record.member_path,
                start_line=record.start_line,
                lines=record.end_line - record.start_line + 1,
                bytes=len(record.text.encode("utf-8")),
                reason=record.oversize_reason,
            )
        added = self._enqueue(
            task,
            buffer,
            text=record.text,
            line_no=record.start_line,
            member_path=record.member_path,
            file_name=record.file_name,
            truncated=record.truncated,
        )
        self._flush_batch(task, buffer)
        return added

    def _flush_multiline_member(
        self,
        task: ClaimedTask,
        assembler: RecordAssembler,
        buffer: list[MatchRecord],
        member_path: str,
        is_match: bool,
        had_lines: bool,
    ) -> int:
        if not is_match:
            return 0
        added = 0
        for record in assembler.flush():
            added += self._emit_record(task, buffer, record)
        if had_lines and assembler.start_matches == 0:
            self._log(
                "warning",
                "record_start_not_found",
                member_path=member_path,
                lines=assembler.preamble_lines,
            )
        return added
```

4. 把 `process()` 中 `for line in scan.lines:` 到 `if buffer:` 之前的整段替换为：

```python
            if self._rules.record_start is None:
                for line in scan.lines:
                    processed_lines += 1
                    if self._member_spec.match_file(line.member_path):
                        matched_rows += self._enqueue(
                            task,
                            buffer,
                            text=line.text,
                            line_no=line.line_no,
                            member_path=line.member_path,
                            file_name=line.file_name,
                            truncated=line.truncated,
                        )
                        self._flush_batch(task, buffer)
                    last_heartbeat = self._tick(
                        task, processed_lines, matched_rows, last_heartbeat
                    )
            else:
                assembler = RecordAssembler(
                    self._rules.record_start,
                    max_lines=self._record_max_lines,
                    max_bytes=self._record_max_bytes,
                )
                current_member: str | None = None
                member_is_match = False
                member_had_lines = False
                for line in scan.lines:
                    processed_lines += 1
                    # line_no 在每个成员内从 1 重新计数，同名成员也能切出边界。
                    if line.member_path != current_member or line.line_no == 1:
                        if current_member is not None:
                            matched_rows += self._flush_multiline_member(
                                task,
                                assembler,
                                buffer,
                                current_member,
                                member_is_match,
                                member_had_lines,
                            )
                            assembler.reset_member()
                        current_member = line.member_path
                        member_is_match = self._member_spec.match_file(current_member)
                        member_had_lines = False
                    member_had_lines = True
                    if member_is_match:
                        for record in assembler.feed(line):
                            matched_rows += self._emit_record(task, buffer, record)
                    last_heartbeat = self._tick(
                        task, processed_lines, matched_rows, last_heartbeat
                    )
                if current_member is not None:
                    matched_rows += self._flush_multiline_member(
                        task,
                        assembler,
                        buffer,
                        current_member,
                        member_is_match,
                        member_had_lines,
                    )
```

5. 在 `except PermanentTaskFailure` 之前（或之后、泛型 `except Exception` 之前）增加：

```python
        except RecordStartError as exc:
            # 起始正则超时是规则/数据问题：重试同一归档只会再次失败。
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

6. `worker/main.py`：

- `WorkerRuntime.__init__` 增加参数 `record_max_lines: int = 2000`、`record_max_bytes: int = 1_048_576`，并保存到同名私有字段。
- 构造 `TaskProcessor` 时传入：

```python
                    record_max_lines=self._record_max_lines,
                    record_max_bytes=self._record_max_bytes,
                    logger=self._logger,
```

- `main()` 构造 `WorkerRuntime` 时传入：

```python
        record_max_lines=settings.record_max_lines,
        record_max_bytes=settings.record_max_bytes,
```

- [ ] **Step 4: 运行确认通过**

Run:
```bash
cd service
uv run pytest -q tests/application/worker/test_processor.py tests/worker/test_runtime.py tests/application/worker/test_records.py
uv run ruff check .
uv run mypy
```
Expected: PASS、ruff/mypy 干净

- [ ] **Step 5: 提交**

```bash
git add service/src/application/worker/processor.py service/src/config/settings.py service/src/worker/main.py service/tests/application/worker/test_processor.py service/tests/worker/test_runtime.py
git commit -m "feat: Worker 按记录评估多行日志并约束记录上限"
```

---

### Task 4: 规则集 API 校验与持久化测试

**Files:**
- Test: `service/tests/api/test_rule_sets.py`

**Interfaces:**
- Consumes: Task 1 的模型校验与编译错误；现有 `POST /api/rule-sets`、`GET /api/rule-sets/{id}`。
- Produces: 无新端点、无新响应字段，仅验证 JSON 配置往返。

- [ ] **Step 1: 写失败测试**

在 `service/tests/api/test_rule_sets.py` 追加：

```python
def test_rule_set_persists_and_returns_record_start_regex(client, auth_headers) -> None:
    config_payload = make_config() | {"record_start_regex": r"^\d{4}-\d{2}-\d{2} "}
    created = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={"name": "multiline", "config": config_payload},
    )
    assert created.status_code == 201
    rule_set_id = created.json()["id"]

    detail = client.get(f"/api/rule-sets/{rule_set_id}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["config"]["record_start_regex"] == r"^\d{4}-\d{2}-\d{2} "


def test_rule_set_without_record_start_regex_round_trips_as_null(
    client, auth_headers
) -> None:
    created = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={"name": "single", "config": make_config()},
    )
    rule_set_id = created.json()["id"]

    detail = client.get(f"/api/rule-sets/{rule_set_id}", headers=auth_headers)
    assert detail.json()["config"]["record_start_regex"] is None


def test_legacy_config_without_record_start_regex_is_readable(
    client, auth_headers, session
) -> None:
    legacy = RuleSet(
        name="legacy",
        version=1,
        config=make_config(),
        created_by="admin",
    )
    session.add(legacy)
    session.commit()
    session.refresh(legacy)

    detail = client.get(f"/api/rule-sets/{legacy.id}", headers=auth_headers)
    assert detail.status_code == 200
    assert "record_start_regex" not in detail.json()["config"]
    parsed = RuleSetConfig.model_validate(detail.json()["config"])
    assert parsed.record_start_regex is None
```

并在现有 `test_invalid_runtime_config_returns_422` 的参数列表中追加两个 case：

```python
        pytest.param(
            make_config() | {"record_start_regex": "   "},
            id="blank-record-start-regex",
        ),
        pytest.param(
            make_config() | {"record_start_regex": "("},
            id="invalid-record-start-regex",
        ),
```

- [ ] **Step 2: 运行确认失败**

Run: `cd service && uv run pytest -q tests/api/test_rule_sets.py`
Expected: 新增用例中至少持久化/往返用例 FAIL（旧实现丢字段）；非法正则用例 FAIL（未校验）

- [ ] **Step 3: 确认源文件无需改动**

这些用例只依赖 Task 1：纯空白由 pydantic 请求体校验返回 422；非法正则由 `create_rule_set_version` → `validate_rule_set_config` → `compile_ruleset` 抛 `ValueError`，`routes_rule_sets.py:34-35` 已映射为 422。不要修改路由或服务代码。

- [ ] **Step 4: 运行确认通过**

Run: `cd service && uv run pytest -q tests/api/test_rule_sets.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add service/tests/api/test_rule_sets.py
git commit -m "test: 覆盖多行起始正则的持久化与 422 校验"
```

---

### Task 5: 前端表单字段与命中换行展示

**Files:**
- Modify: `web/src/api/logharvestTypes.ts:203-209`
- Modify: `web/src/views/logharvest/rulesets/edit-drawer.vue:74-84, 151-161, 217-226, 260-275`
- Modify: `web/src/views/logharvest/matches/index.vue:259-266, 292-299`

**Interfaces:**
- Consumes: 后端 `RuleSetConfig.record_start_regex?: string | null`。
- Produces: 表单留空提交 `null`；旧版本回显 `?? ""`；`matches.raw_line` 保留记录内换行。

- [ ] **Step 1: 更新类型**

`logharvestTypes.ts`：

```ts
export type RuleSetConfig = {
  name: string;
  file_glob: string;
  encoding: string;
  fallback_encoding: string;
  /** M3 多行记录：命中起始行后把后续行并入记录；null/缺省为单行模式 */
  record_start_regex?: string | null;
  rules: Rule[];
};
```

- [ ] **Step 2: 表单增加字段**

`edit-drawer.vue`：

1. `form` 增加 `record_start_regex: ""`。
2. `fillFrom` 增加 `form.record_start_regex = source.config.record_start_regex ?? "";`。
3. 提交 config 增加：

```ts
        record_start_regex: form.record_start_regex.trim()
          ? form.record_start_regex
          : null,
```

4. 在“回退编码”表单项之后插入：

```vue
      <el-form-item label="记录起始正则">
        <el-input
          v-model="form.record_start_regex"
          placeholder="留空为单行模式，例如 ^\d{4}-\d{2}-\d{2}"
        />
        <div class="hint">
          命中起始行后，后续非起始行并入同一条记录再执行规则；建议用 ^
          锚定。留空保持单行模式。多行规则集必须先把全部 Worker 升级到支持
          M3 的版本再创建。
        </div>
      </el-form-item>
```

- [ ] **Step 3: 命中展示**

`matches/index.vue`：

1. tooltip 文案改为：

```vue
                content="原始记录超过存储/匹配上限，后端只保存截断内容（raw_truncated = true）"
```

2. `.match-raw` 增加 `white-space: pre-wrap;`（保留 3 行折叠）。

- [ ] **Step 4: 前端门禁**

Run: `cd web && pnpm typecheck && pnpm build`
Expected: 均通过。本机内置浏览器为隐藏页，**不做视觉确认**。

- [ ] **Step 5: 提交**

```bash
git add web/src/api/logharvestTypes.ts web/src/views/logharvest/rulesets/edit-drawer.vue web/src/views/logharvest/matches/index.vue
git commit -m "feat: 规则集表单支持记录起始正则"
```

---

### Task 6: 文档、发布顺序与全量验收

**Files:**
- Modify: `service/README.md:58-80, 195-210, 300-335`
- Modify: `README.md:55-62`
- Modify: `docs/superpowers/specs/2026-10-26-logharvest-m3-multiline-records-design.md:1-3`

**Interfaces:**
- Consumes: Task 1-5 的全部行为。
- Produces: 可交付文档与验收证据。

- [ ] **Step 1: 更新 service/README**

1. 配置表在 `LOGHARVEST_MATCHES_BATCH_SIZE` 之后增加：

```markdown
| `LOGHARVEST_RECORD_MAX_LINES` | `2000` | M3 多行模式单条记录最多累积的物理行数，超过后 flush 已累积内容并跳过后续续行直到下一条起始行 |
| `LOGHARVEST_RECORD_MAX_BYTES` | `1048576` | M3 多行模式单条记录最多累积的 UTF-8 字节数（含换行分隔符），超过后的行为同上 |
```

2. Worker 流程第 6 条改为“流式解压与逐行/逐记录匹配”，并增加一段：

```markdown
多行记录（M3）：规则集配置可选 `record_start_regex`。命中起始行后用 `RecordAssembler` 把后续非起始行并入同一记录，直到下一条起始行或成员结束；规则对整条记录只评估一次，`matches.line_no` 是记录起始行，`processed_lines` 仍是物理行数。记录不跨成员，preamble 不评估；成员内无起始行时输出 `record_start_not_found` warning。记录超过 `LOGHARVEST_RECORD_MAX_LINES`/`LOGHARVEST_RECORD_MAX_BYTES` 时 flush 已累积部分并标 `raw_truncated=True`，输出 `record_oversized` warning，任务不失败。起始正则运行时超时（50 ms）任务永久失败。发布顺序：先滚动升级全部 Worker，再创建带 `record_start_regex` 的规则集，最后创建作业；旧 Worker 会忽略该字段并按单行模式产生错误结果。
```

3. 在 `### 明确不包含（后续里程碑）` 中删除 `- 多行堆栈规则；v1 保持单行优先。` 这一条，并在 `### M2a（已交付）` 之后新增 `### M3（已交付）` 段落，内容与上面的多行记录说明一致（含两个限额变量与升级顺序）。

- [ ] **Step 2: 更新根 README**

- 在 `## 仓库状态` 的成员级容错条目之后增加 M3 条目：`record_start_regex` 记录组装、记录级评估、两个上限、起始正则 422/超时永久失败、前端表单字段与 Worker 升级顺序。
- “尚未实现”列表删除“多行堆栈规则”，把“下一步”改为部署编排与运维侧事项。

- [ ] **Step 3: 更新 spec 状态**

把 spec 头部状态改为 `已完成（2026-10-26）`，并在验收标准勾选实际达成项（未做视觉确认的保持未勾选或注明）。

- [ ] **Step 4: 全量验收**

Run:
```bash
cd service
uv run ruff check .
uv run mypy
uv run pytest -q
LOGHARVEST_TEST_PG_URL="postgresql+psycopg://logharvest:logharvest@127.0.0.1:5433/logharvest" \
  uv run pytest -q tests/integration

cd ../web
pnpm typecheck
pnpm build
```
Expected: 全部通过；把实际 SQLite/PG 数字写入 spec 与 README 的验证口径。

- [ ] **Step 5: 提交**

```bash
git add README.md service/README.md docs/superpowers/specs/2026-10-26-logharvest-m3-multiline-records-design.md docs/superpowers/plans/2026-10-26-logharvest-m3-multiline-records.md
git commit -m "docs: 记录 M3 多行记录的配置、语义与升级顺序"
```

---

## 验收清单

- [ ] 旧规则集行为不变，现有全部测试通过。
- [ ] 起始/续行/嵌套起始/空行/EOF/成员边界语义符合 spec §5。
- [ ] 多行模式每条记录只评估一次，`line_no` 为起始行，`processed_lines` 为物理行数。
- [ ] 行数/字节上限、单行超限、跳过态、`raw_truncated`、`record_oversized` 可验证。
- [ ] 起始正则空白/非法 → 422；运行时超时 → `retryable=False`。
- [ ] 损坏成员先 flush 已累积记录再按 `partial`/`failed` 收尾。
- [ ] API 对旧/新配置均可读；前端表单可录入/回显/保存。
- [ ] `matches` 保留换行展示与截断提示，不新增列。
- [ ] ruff、mypy、SQLite 全量、PG 集成、前端 typecheck/build 全绿。
