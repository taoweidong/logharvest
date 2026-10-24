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
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if heartbeat_every_lines < 1:
            raise ValueError("heartbeat_every_lines must be >= 1")
        self._repo = repo
        self._writer = writer
        self._rules = rules
        self._work_dir = Path(work_dir)
        self._batch_size = batch_size
        self._heartbeat_every_lines = heartbeat_every_lines

    def process(self, task: ClaimedTask) -> TaskResult:
        processed_lines = 0
        matched_rows = 0
        buffer: list[MatchRecord] = []
        task_dir = self._work_dir / str(task.id)

        try:
            if task.attempts > 1:
                self._writer.delete_for_task(task.id)

            task_dir.mkdir(parents=True, exist_ok=True)
            local_path = self._copy_to_local(task, task_dir)

            for line in iter_archive_lines(
                local_path,
                encoding=task.encoding,
                fallback_encoding=task.fallback_encoding,
            ):
                processed_lines += 1
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
                    self._writer.write_batch(buffer)
                    buffer.clear()

                if processed_lines % self._heartbeat_every_lines == 0 and not self._repo.heartbeat(
                    task.id, task.attempts, processed_lines, matched_rows
                ):
                    raise LeaseLost(f"lease lost for task {task.id}")

            if buffer:
                self._writer.write_batch(buffer)
            self._repo.complete(task.id, task.attempts, "succeeded", processed_lines, matched_rows)
            return TaskResult("succeeded", processed_lines, matched_rows)
        except LeaseLost:
            raise
        # Boundary of the worker loop: any ordinary failure must be recorded on
        # the task instead of aborting the process.
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
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

    def _copy_to_local(self, task: ClaimedTask, task_dir: Path) -> Path:
        source = task.source_path
        source_size = source.stat().st_size
        target = task_dir / source.name
        with source.open("rb") as source_file, target.open("wb") as target_file:
            shutil.copyfileobj(source_file, target_file, length=COPY_CHUNK_SIZE)
        copied_size = target.stat().st_size
        if copied_size != source_size:
            raise OSError(
                f"incomplete copy for task {task.id}: "
                f"expected {source_size} bytes, copied {copied_size}"
            )
        return target
