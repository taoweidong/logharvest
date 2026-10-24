import shutil
from collections.abc import Callable
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
from src.domain.archive.reader import iter_archive_lines
from src.domain.rules.evaluator import CompiledRuleSet, evaluate_line

RAW_LINE_LIMIT = 4096
COPY_CHUNK_SIZE = 1024 * 1024


class LeaseLost(RuntimeError):
    pass


def _truncate_raw_line(text: str) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= RAW_LINE_LIMIT:
        return text, False
    # errors="ignore" only drops an incomplete multi-byte sequence at the cut.
    return encoded[:RAW_LINE_LIMIT].decode("utf-8", errors="ignore"), True


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
        self._member_spec = pathspec.PathSpec.from_lines("gitwildmatch", [rules.config.file_glob])

    def process(self, task: ClaimedTask) -> TaskResult:
        processed_lines = 0
        matched_rows = 0
        last_heartbeat = self._clock()
        buffer: list[MatchRecord] = []
        task_dir = self._work_dir / str(task.id)
        encoding = task.encoding or self._rules.config.encoding
        fallback_encoding = task.fallback_encoding or self._rules.config.fallback_encoding

        result: TaskResult
        try:
            if task.attempts > 1:
                self._writer.delete_for_task(task.id)

            task_dir.mkdir(parents=True, exist_ok=True)
            local_path = self._copy_to_local(task, task_dir)

            for line in iter_archive_lines(
                local_path,
                encoding=encoding,
                fallback_encoding=fallback_encoding,
            ):
                processed_lines += 1
                if self._member_spec.match_file(line.member_path):
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
                    last_heartbeat = now

            if buffer:
                self._writer.write_batch(tuple(buffer))
            result = TaskResult("succeeded", processed_lines, matched_rows)
        except LeaseLost:
            raise
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
            self._repo.complete(task.id, task.attempts, "succeeded", processed_lines, matched_rows)
            return result
        finally:
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
                if not self._repo.touch(task.id, task.attempts):
                    raise LeaseLost(f"lease lost for task {task.id}")
                last_touch = now
