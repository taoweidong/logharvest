import shutil
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
from time import monotonic
from typing import BinaryIO

import pathspec

from src.application.worker.ports import (
    ClaimedTask,
    MatchRecord,
    MatchWriter,
    TaskRepository,
    TaskResult,
)
from src.domain.archive.reader import (
    DEFAULT_ARCHIVE_BUDGET,
    ArchiveBudget,
    ArchiveMemberError,
    ArchiveScan,
    scan_archive,
)
from src.domain.rules.evaluator import CompiledRuleSet, evaluate_line
from src.domain.source_guard import POLICY_ENFORCE, SourceNotReadOnly, guard_source

RAW_LINE_LIMIT = 4096
COPY_CHUNK_SIZE = 1024 * 1024
ERROR_TEXT_LIMIT = 2000


class LeaseLost(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    """The owning job asked to be cancelled; stop and record a cancelled task."""


class PermanentTaskFailure(RuntimeError):
    """Source changed or vanished; retrying would fail the same way, so do not retry."""


def _truncate_raw_line(text: str) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= RAW_LINE_LIMIT:
        return text, False
    # errors="ignore" only drops an incomplete multi-byte sequence at the cut.
    return encoded[:RAW_LINE_LIMIT].decode("utf-8", errors="ignore"), True


def _format_member_errors(errors: list[ArchiveMemberError]) -> str:
    details = "; ".join(f"{error.member_path or '<archive>'}: {error.message}" for error in errors)
    text = f"{len(errors)} member(s) failed: {details}"
    if len(text) <= ERROR_TEXT_LIMIT:
        return text
    return text[: ERROR_TEXT_LIMIT - 3] + "..."


class TaskProcessor:
    def __init__(
        self,
        repo: TaskRepository,
        writer: MatchWriter,
        rules: CompiledRuleSet,
        work_dir: Path,
        batch_size: int = 1000,
        heartbeat_every_lines: int = 20_000,
        heartbeat_every_seconds: float = 5.0,
        copy_heartbeat_seconds: float = 5.0,
        clock: Callable[[], float] = monotonic,
        on_lease_refresh: Callable[[], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        nas_read_only_policy: str = POLICY_ENFORCE,
        budget: ArchiveBudget | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if heartbeat_every_lines < 1:
            raise ValueError("heartbeat_every_lines must be >= 1")
        if heartbeat_every_seconds < 0:
            raise ValueError("heartbeat_every_seconds must be >= 0")
        if copy_heartbeat_seconds < 0:
            raise ValueError("copy_heartbeat_seconds must be >= 0")
        self._repo = repo
        self._writer = writer
        self._rules = rules
        self._work_dir = Path(work_dir)
        self._batch_size = batch_size
        self._heartbeat_every_lines = heartbeat_every_lines
        self._heartbeat_every_seconds = heartbeat_every_seconds
        self._copy_heartbeat_seconds = copy_heartbeat_seconds
        self._clock = clock
        self._on_lease_refresh = on_lease_refresh
        self._cancel_check = cancel_check
        self._nas_read_only_policy = nas_read_only_policy
        self._budget = budget if budget is not None else DEFAULT_ARCHIVE_BUDGET
        self._member_spec = pathspec.PathSpec.from_lines("gitwildmatch", [rules.config.file_glob])

    def _verify_lease(self, task: ClaimedTask) -> None:
        """Confirm the lease immediately before any database write.

        The spec uses ``(file_task_id, attempts)`` as a lightweight write fence:
        a worker whose task was recovered must stop instead of appending rows a
        retry would not clean up again.
        """
        if not self._repo.touch(task.id, task.attempts):
            raise LeaseLost(f"lease lost for task {task.id}")
        if self._on_lease_refresh is not None:
            self._on_lease_refresh()

    def _check_root(self, task: ClaimedTask) -> None:
        """Re-verify that the source still lives under its data source root.

        规格 §11 要求"扫描和复制时使用 realpath 校验，禁止跟随指向根路径外的符号
        链接"。规划期（`jobs.create_job`）已经用 ``resolve()`` 判过一次，但
        ``file_tasks.source_path`` 只是当时记下的字符串：规划与执行之间源文件可能
        被换成指向根外的符号链接。`stat()` 与 `open()` 都会跟随符号链接，所以执行
        期必须按 realpath 再判一次，否则 Worker 会把越界内容复制并写进 `matches`。
        """
        if task.root_path is None:
            return
        root = Path(task.root_path).resolve()
        try:
            real_source = task.source_path.resolve()
        except OSError as exc:
            raise PermanentTaskFailure(f"source unavailable: {exc}") from exc
        if not real_source.is_relative_to(root):
            raise PermanentTaskFailure(
                f"source escapes data source root: {real_source} is not under {root}"
            )

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
        if task.expected_mtime is not None:
            expected = task.expected_mtime
            if expected.tzinfo is None:
                # The column is `timestamp without time zone`: PostgreSQL hands back
                # UTC wall time as a naive datetime, and naive.timestamp() would read it
                # as local time, so every task would look permanently stale off UTC.
                expected = expected.replace(tzinfo=UTC)
            if int(stat.st_mtime) != int(expected.timestamp()):
                raise PermanentTaskFailure("source mtime changed since planning")

    def _member_outcome(self, scan: ArchiveScan) -> tuple[str, str | None]:
        relevant_errors = [
            error
            for error in scan.member_errors
            if error.scan_incomplete
            or error.member_path is None
            or self._member_spec.match_file(error.member_path)
        ]
        if not relevant_errors:
            return "succeeded", None
        succeeded = any(self._member_spec.match_file(member) for member in scan.succeeded_members)
        return ("partial" if succeeded else "failed"), _format_member_errors(relevant_errors)

    def process(self, task: ClaimedTask) -> TaskResult:
        processed_lines = 0
        matched_rows = 0
        last_heartbeat = self._clock()
        buffer: list[MatchRecord] = []
        task_dir = self._work_dir / str(task.id)
        encoding = task.encoding or self._rules.config.encoding
        fallback_encoding = task.fallback_encoding or self._rules.config.fallback_encoding

        result: TaskResult
        scan: ArchiveScan | None = None
        try:
            # 只读守卫排在最前面：可写挂载上连 stat、租约栅栏和本地复制都不该发生
            # （规格 §7.2）。判定按压缩包所在目录，嵌套挂载可以各自不同。
            guard_source(task.source_path.parent, policy=self._nas_read_only_policy)
            # 越界校验排在任何 stat/open 之前：路径一旦逃出数据源根，
            # 后面的校验与复制都会作用在越界内容上（规格 §11）。
            self._check_root(task)
            # A source that vanished or changed since planning must not be
            # retried; fail it permanently before spending a lease fence or copy.
            self._check_source(task)
            # Fence before the retry cleanup too: deleting another attempt's
            # rows is a write as well.
            self._verify_lease(task)
            if task.attempts > 1:
                self._writer.delete_for_task(task.id)

            task_dir.mkdir(parents=True, exist_ok=True)
            local_path = self._copy_to_local(task, task_dir)

            scan = scan_archive(
                local_path,
                encoding=encoding,
                fallback_encoding=fallback_encoding,
                budget=self._budget,
            )
            # ``file_glob`` 只和成员路径有关，而成员路径在整个成员期间不变。
            # 放到逐行热路径上会让 glob 匹配次数与总行数成正比（实测 702ns/行
            # vs 缓存后 60ns/行）；这里只在成员切换时算一次。
            current_member: str | None = None
            member_matched = False
            for line in scan.lines:
                processed_lines += 1
                if line.member_path != current_member:
                    current_member = line.member_path
                    member_matched = bool(self._member_spec.match_file(line.member_path))
                if member_matched:
                    matches = evaluate_line(self._rules, line.text, line.line_no)
                    if matches:
                        raw_line, raw_truncated = _truncate_raw_line(line.text)
                        raw_truncated = raw_truncated or line.truncated
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
                                    member_path=line.member_path,
                                    file_name=line.file_name,
                                    line_no=line.line_no,
                                    raw_line=raw_line,
                                    raw_truncated=raw_truncated,
                                    fields=dict(match.fields),
                                    numeric_value=match.numeric_value,
                                )
                            )
                            matched_rows += 1

                    if len(buffer) >= self._batch_size:
                        self._verify_lease(task)
                        self._writer.write_batch(tuple(buffer))
                        buffer.clear()

                now = self._clock()
                if (
                    processed_lines % self._heartbeat_every_lines == 0
                    or now - last_heartbeat >= self._heartbeat_every_seconds
                ):
                    if not self._repo.heartbeat(
                        task.id, task.attempts, processed_lines, matched_rows
                    ):
                        raise LeaseLost(f"lease lost for task {task.id}")
                    # 规格 §7.3 要求每 2 万行查一次取消；放在心跳预算里既满足这个频率，
                    # 又让慢行（不足 2 万行）最迟 5 秒也能停下来。逐行查会变成逐行 SELECT。
                    if self._cancel_check is not None and self._cancel_check():
                        raise JobCancelled(f"job {task.job_id} cancelled")
                    if self._on_lease_refresh is not None:
                        self._on_lease_refresh()
                    last_heartbeat = now

            if buffer:
                self._verify_lease(task)
                self._writer.write_batch(tuple(buffer))
            status, member_error = self._member_outcome(scan)
            result = TaskResult(status, processed_lines, matched_rows, member_error)
        except LeaseLost:
            raise
        except JobCancelled:
            self._repo.complete(task.id, task.attempts, "cancelled", processed_lines, matched_rows)
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
        except SourceNotReadOnly as exc:
            # 可写挂载是环境事实，不是抖动：重试只会同样失败并烧光 max_attempts。
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
        # Boundary of the worker loop: any ordinary failure must be recorded on
        # the task instead of aborting the process. Successful completion stays
        # outside this handler so its own failure cannot trigger a failed
        # completion for a task that already succeeded.
        except Exception as exc:  # noqa: BLE001
            self._repo.complete(
                task.id,
                task.attempts,
                "failed",
                processed_lines,
                matched_rows,
                str(exc),
            )
            return TaskResult("failed", processed_lines, matched_rows, str(exc))
        else:
            if result.status == "succeeded":
                self._repo.complete(
                    task.id, task.attempts, "succeeded", processed_lines, matched_rows
                )
            else:
                # A damaged member is a content failure: retrying the same
                # archive would fail again and only burn attempts.
                self._repo.complete(
                    task.id,
                    task.attempts,
                    result.status,
                    processed_lines,
                    matched_rows,
                    result.error,
                    retryable=False,
                )
            return result
        finally:
            # The scan generator owns the archive handle; close it before the
            # work directory is removed or Windows keeps the file locked.
            if scan is not None:
                close = getattr(scan.lines, "close", None)
                if close is not None:
                    close()
            shutil.rmtree(task_dir, ignore_errors=True)

    def _copy_to_local(self, task: ClaimedTask, task_dir: Path) -> Path:
        source = task.source_path
        source_size = source.stat().st_size
        target = task_dir / source.name
        with source.open("rb") as source_file, target.open("wb") as target_file:
            self._copy_chunks(source_file, target_file, task)
        copied_size = target.stat().st_size
        if copied_size != source_size:
            raise OSError(
                f"incomplete copy for task {task.id}: "
                f"expected {source_size} bytes, copied {copied_size}"
            )
        return target

    def _copy_chunks(self, source_file: BinaryIO, target_file: BinaryIO, task: ClaimedTask) -> None:
        """Copy in chunks, touching the lease regularly.

        Copying a multi-gigabyte archive can take longer than the stale-recovery
        threshold, so the worker refreshes ``heartbeat_at`` while no lines are
        flowing yet. A fence miss means recovery already reassigned the task.
        """
        last_touch = self._clock()
        while chunk := source_file.read(COPY_CHUNK_SIZE):
            target_file.write(chunk)
            now = self._clock()
            if now - last_touch >= self._copy_heartbeat_seconds:
                self._verify_lease(task)
                last_touch = now
