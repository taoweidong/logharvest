import io
import tarfile
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import src.application.worker.processor as processor_module
from src.application.worker.ports import ClaimedTask, MatchRecord
from src.application.worker.processor import LeaseLost, TaskProcessor
from src.domain import source_guard
from src.domain.archive import ArchiveMemberError
from src.domain.rules.evaluator import compile_ruleset
from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleSetConfig,
    Threshold,
)
from src.domain.source_guard import UNKNOWN, WRITABLE


class FakeRepo:
    def __init__(self) -> None:
        self.heartbeats: list[tuple[int, int]] = []
        self.touches: list[tuple[int, int]] = []
        self.completed: list[tuple[int, int, str, int, int, str | None]] = []
        self.retryables: list[tuple[int, bool]] = []
        self.heartbeat_result = True
        self.touch_result = True

    def touch(self, task_id: int, attempts: int) -> bool:
        self.touches.append((task_id, attempts))
        return self.touch_result

    def heartbeat(
        self, task_id: int, attempts: int, processed_lines: int, matched_rows: int
    ) -> bool:
        self.heartbeats.append((processed_lines, matched_rows))
        return self.heartbeat_result

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
        self.completed.append((task_id, attempts, status, processed_lines, matched_rows, error))
        self.retryables.append((task_id, retryable))


class SteppingClock:
    """Monotonic replacement that advances on every call."""

    def __init__(self, step: float) -> None:
        self.step = step
        self.value = 0.0

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class FakeWriter:
    def __init__(self) -> None:
        self.deleted: list[int] = []
        self.written: list[MatchRecord] = []

    def delete_for_task(self, task_id: int) -> None:
        self.deleted.append(task_id)

    def write_batch(self, matches) -> None:
        self.written.extend(matches)


class RetainingWriter(FakeWriter):
    """Keeps every batch object as passed, instead of copying its records."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list = []

    def write_batch(self, matches) -> None:
        self.batches.append(matches)


class SuccessCompletionFailingRepo(FakeRepo):
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
        self.completed.append((task_id, attempts, status, processed_lines, matched_rows, error))
        self.retryables.append((task_id, retryable))
        if status == "succeeded":
            raise RuntimeError("completion write failed")


def write_zip(path: Path, members: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return path


def write_deflated_zip(path: Path, members: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return path


def corrupt_zip_member(path: Path, member_name: str) -> None:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member_name)
        data_start = (
            info.header_offset + 30 + len(info.filename.encode("utf-8")) + len(info.extra)
        )
    with path.open("r+b") as stream:
        stream.seek(data_start)
        original = stream.read(1)
        stream.seek(data_start)
        stream.write(bytes([original[0] ^ 0xFF]))


def truncate_tar_member(path: Path, member_name: str) -> None:
    with tarfile.open(path) as archive:
        info = archive.getmember(member_name)
        cut = info.offset_data + info.size // 2
    path.write_bytes(path.read_bytes()[:cut])


def make_task(
    tmp_path: Path,
    attempts: int = 1,
    members: dict[str, str | bytes] | None = None,
    **overrides,
) -> ClaimedTask:
    if members is None:
        members = {"app.log": "ERROR one\nelapsed=6000ms\n"}
    archive = write_zip(tmp_path / "a.zip", members)
    options = {
        "id": 10,
        "job_id": 20,
        "source_path": archive,
        "file_name": "a.zip",
        "attempts": attempts,
        "rule_set_id": 30,
        "rule_version": 1,
    }
    options.update(overrides)
    return ClaimedTask(**options)


def make_rules(
    *,
    file_glob: str = "**/*",
    encoding: str = "utf-8",
    fallback_encoding: str = "gb18030",
    keyword: str = "ERROR",
):
    return compile_ruleset(
        RuleSetConfig(
            name="t",
            file_glob=file_glob,
            encoding=encoding,
            fallback_encoding=fallback_encoding,
            rules=[
                Rule(id="error", match=KeywordMatch(any=[keyword]), severity="error"),
                Rule(
                    id="slow",
                    match=RegexMatch(pattern=r"elapsed=(?P<elapsed_ms>\d+)ms"),
                    threshold=Threshold(field="elapsed_ms", op="gt", value=Decimal(5000)),
                    severity="warning",
                ),
            ],
        )
    )


def make_processor(
    repo: FakeRepo,
    writer: FakeWriter,
    work_dir: Path,
    rules=None,
    **overrides,
) -> TaskProcessor:
    options = {
        "batch_size": 1,
        "heartbeat_every_lines": 1,
    }
    options.update(overrides)
    return TaskProcessor(
        repo=repo,
        writer=writer,
        rules=rules or make_rules(),
        work_dir=work_dir,
        **options,
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"batch_size": 0}, "batch_size"),
        ({"heartbeat_every_lines": 0}, "heartbeat_every_lines"),
        ({"heartbeat_every_seconds": -1}, "heartbeat_every_seconds"),
        ({"copy_heartbeat_seconds": -1}, "copy_heartbeat_seconds"),
    ],
)
def test_processor_rejects_invalid_timing_config(
    tmp_path: Path, overrides: dict, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        make_processor(FakeRepo(), FakeWriter(), tmp_path / "work", **overrides)


def test_member_error_summary_is_bounded() -> None:
    errors = [ArchiveMemberError(f"member-{index}.log", "x" * 100) for index in range(30)]

    summary = processor_module._format_member_errors(errors)

    assert len(summary) == processor_module.ERROR_TEXT_LIMIT
    assert summary.endswith("...")


def test_processor_reads_archive_and_writes_matches(tmp_path: Path) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    work_dir = tmp_path / "work"
    processor = TaskProcessor(
        repo=repo,
        writer=writer,
        rules=make_rules(),
        work_dir=work_dir,
        batch_size=1,
        heartbeat_every_lines=1,
    )
    task = make_task(tmp_path)
    original_bytes = task.source_path.read_bytes()

    result = processor.process(task)

    assert result.status == "succeeded"
    assert result.processed_lines == 2
    assert result.matched_rows == 2
    assert [(m.rule_id, m.member_path, m.line_no) for m in writer.written] == [
        ("error", "app.log", 1),
        ("slow", "app.log", 2),
    ]
    assert writer.written[1].fields["elapsed_ms"] == "6000"
    assert writer.written[1].numeric_value == Decimal(6000)
    assert writer.written[0].rule_set_id == 30
    assert writer.written[0].rule_version == 1
    assert writer.written[0].job_id == 20
    assert writer.written[0].file_task_id == 10
    assert writer.written[0].source_path == str(task.source_path)
    assert writer.written[0].raw_truncated is False
    assert repo.completed == [(10, 1, "succeeded", 2, 2, None)]
    assert writer.deleted == []
    assert task.source_path.read_bytes() == original_bytes
    assert not (work_dir / "10").exists()


def test_retry_deletes_old_matches(tmp_path: Path) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    processor = TaskProcessor(
        repo=repo, writer=writer, rules=make_rules(), work_dir=tmp_path / "work"
    )
    processor.process(make_task(tmp_path, attempts=2))
    assert writer.deleted == [10]


def test_tar_gz_copy_keeps_original_name_and_processes_members(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "a.tar.gz"
    payload = b"ERROR one\nelapsed=6000ms\n"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo("nested/app.log")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))

    captured: list[Path] = []
    real_scan = processor_module.scan_archive

    def spy(path, **kwargs):
        captured.append(Path(path))
        return real_scan(path, **kwargs)

    monkeypatch.setattr(processor_module, "scan_archive", spy)

    repo = FakeRepo()
    writer = FakeWriter()
    work_dir = tmp_path / "work"
    task = ClaimedTask(
        id=10,
        job_id=20,
        source_path=archive,
        file_name="a.tar.gz",
        attempts=1,
        rule_set_id=30,
        rule_version=1,
    )

    result = make_processor(repo, writer, work_dir).process(task)

    assert result.status == "succeeded"
    assert len(captured) == 1
    assert captured[0].name == "a.tar.gz"
    assert captured[0].parent == work_dir / "10"
    assert not captured[0].exists()
    assert not captured[0].parent.exists()
    assert [(m.rule_id, m.member_path, m.line_no) for m in writer.written] == [
        ("error", "nested/app.log", 1),
        ("slow", "nested/app.log", 2),
    ]
    assert repo.completed == [(10, 1, "succeeded", 2, 2, None)]


def test_raw_line_is_truncated_to_4096_bytes(tmp_path: Path) -> None:
    long_ascii = "ERROR " + "x" * 4090 + "y"
    multibyte = "ERROR " + "中" * 3000
    archive = tmp_path / "long.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("app.log", f"{long_ascii}\n{multibyte}\n")

    rules = compile_ruleset(
        RuleSetConfig(
            name="t",
            rules=[Rule(id="error", match=KeywordMatch(any=["ERROR"]))],
        )
    )
    repo = FakeRepo()
    writer = FakeWriter()
    processor = TaskProcessor(
        repo=repo,
        writer=writer,
        rules=rules,
        work_dir=tmp_path / "work",
    )
    result = processor.process(
        ClaimedTask(
            id=12,
            job_id=20,
            source_path=archive,
            file_name="long.zip",
            attempts=1,
            rule_set_id=30,
            rule_version=3,
        )
    )

    assert result.status == "succeeded"
    assert result.matched_rows == 2
    first, second = writer.written
    assert first.raw_line == long_ascii[:4096]
    assert len(first.raw_line.encode("utf-8")) == 4096
    assert first.raw_truncated is True
    assert len(second.raw_line.encode("utf-8")) <= 4096
    assert "\ufffd" not in second.raw_line
    assert second.raw_truncated is True


def test_invalid_archive_completes_failed_and_removes_work_dir(tmp_path: Path) -> None:
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"not a zip")
    repo = FakeRepo()
    writer = FakeWriter()
    work_dir = tmp_path / "work"
    task = ClaimedTask(
        id=11,
        job_id=20,
        source_path=archive,
        file_name="broken.zip",
        attempts=1,
        rule_set_id=30,
        rule_version=1,
    )

    result = make_processor(repo, writer, work_dir).process(task)

    assert result.status == "failed"
    assert result.processed_lines == 0
    assert result.matched_rows == 0
    assert "invalid zip archive" in (result.error or "")
    assert repo.completed == [(11, 1, "failed", 0, 0, result.error)]
    assert writer.written == []
    assert not (work_dir / "11").exists()


def test_partial_when_some_members_are_corrupt(tmp_path: Path) -> None:
    archive = write_deflated_zip(
        tmp_path / "partial.zip",
        {"bad.log": "ERROR bad\n" * 100, "app.log": "ERROR one\n"},
    )
    corrupt_zip_member(archive, "bad.log")
    repo = FakeRepo()
    writer = FakeWriter()
    task = ClaimedTask(
        id=10,
        job_id=20,
        source_path=archive,
        file_name="partial.zip",
        attempts=1,
        rule_set_id=30,
        rule_version=1,
    )

    result = make_processor(repo, writer, tmp_path / "work").process(task)

    assert result.status == "partial"
    assert "bad.log" in (result.error or "")
    assert repo.completed == [(10, 1, "partial", 1, 1, result.error)]
    assert repo.retryables == [(10, False)]
    assert [(match.member_path, match.line_no) for match in writer.written] == [
        ("app.log", 1)
    ]


def test_failed_when_every_member_is_corrupt(tmp_path: Path) -> None:
    archive = write_deflated_zip(
        tmp_path / "broken.zip",
        {"one.log": "ERROR one\n" * 100, "two.log": "ERROR two\n" * 100},
    )
    corrupt_zip_member(archive, "one.log")
    corrupt_zip_member(archive, "two.log")
    repo = FakeRepo()
    writer = FakeWriter()
    task = ClaimedTask(
        id=10,
        job_id=20,
        source_path=archive,
        file_name="broken.zip",
        attempts=1,
        rule_set_id=30,
        rule_version=1,
    )

    result = make_processor(repo, writer, tmp_path / "work").process(task)

    assert result.status == "failed"
    assert "one.log" in (result.error or "")
    assert "two.log" in (result.error or "")
    assert repo.completed == [(10, 1, "failed", 0, 0, result.error)]
    assert repo.retryables == [(10, False)]
    assert writer.written == []


def test_corrupt_member_outside_file_glob_does_not_degrade_status(tmp_path: Path) -> None:
    archive = write_deflated_zip(
        tmp_path / "mixed.zip",
        {"notes.txt": "ERROR notes\n" * 100, "app.log": "ERROR one\n"},
    )
    corrupt_zip_member(archive, "notes.txt")
    repo = FakeRepo()
    writer = FakeWriter()
    task = ClaimedTask(
        id=10,
        job_id=20,
        source_path=archive,
        file_name="mixed.zip",
        attempts=1,
        rule_set_id=30,
        rule_version=1,
    )

    result = make_processor(
        repo,
        writer,
        tmp_path / "work",
        rules=make_rules(file_glob="**/*.log"),
    ).process(task)

    assert result.status == "succeeded"
    assert result.error is None
    assert repo.completed == [(10, 1, "succeeded", 1, 1, None)]
    assert repo.retryables == [(10, True)]
    assert [(match.member_path, match.line_no) for match in writer.written] == [
        ("app.log", 1)
    ]


def test_corrupt_tar_member_outside_glob_cannot_report_success(tmp_path: Path) -> None:
    archive = tmp_path / "mixed.tar"
    with tarfile.open(archive, "w") as tf:
        for name, text in (
            ("notes.txt", "notes\n" * 100),
            ("app.log", "ERROR one\n"),
        ):
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    truncate_tar_member(archive, "notes.txt")
    repo = FakeRepo()
    writer = FakeWriter()
    task = ClaimedTask(
        id=10,
        job_id=20,
        source_path=archive,
        file_name="mixed.tar",
        attempts=1,
        rule_set_id=30,
        rule_version=1,
    )

    result = make_processor(
        repo,
        writer,
        tmp_path / "work",
        rules=make_rules(file_glob="**/*.log"),
    ).process(task)

    # A streaming tar stops at the damaged member, so the matching member
    # behind it was never examined; the task must not claim success.
    assert result.status == "failed"
    assert "notes.txt" in (result.error or "")
    assert repo.retryables == [(10, False)]
    assert writer.written == []


def test_incomplete_copy_fails_task_before_processing(tmp_path: Path, monkeypatch) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    work_dir = tmp_path / "work"

    def short_copy(self, source_file, target_file, task):
        target_file.write(source_file.read(4))

    monkeypatch.setattr(TaskProcessor, "_copy_chunks", short_copy)

    result = make_processor(repo, writer, work_dir).process(make_task(tmp_path))

    assert result.status == "failed"
    assert "copy" in (result.error or "").lower()
    assert repo.completed[0][2] == "failed"
    assert writer.written == []
    assert not (work_dir / "10").exists()


def test_copy_refreshes_lease_with_touch(tmp_path: Path) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    processor = make_processor(
        repo,
        writer,
        tmp_path / "work",
        # A batch larger than the archive means the only fences in play are the
        # pre-write fence, the copy-phase fence and the single tail flush.
        batch_size=10_000,
        heartbeat_every_lines=10_000,
        copy_heartbeat_seconds=5,
        clock=SteppingClock(step=6),
    )

    result = processor.process(make_task(tmp_path))

    assert result.status == "succeeded"
    assert repo.touches == [(10, 1), (10, 1), (10, 1)]
    assert writer.written


def test_copy_stops_when_touch_reports_lost_lease(tmp_path: Path) -> None:
    repo = FakeRepo()
    repo.touch_result = False
    work_dir = tmp_path / "work"
    processor = make_processor(
        repo,
        FakeWriter(),
        work_dir,
        copy_heartbeat_seconds=5,
        clock=SteppingClock(step=6),
    )

    with pytest.raises(LeaseLost):
        processor.process(make_task(tmp_path))

    assert repo.completed == []
    assert not (work_dir / "10").exists()


@pytest.mark.parametrize(
    ("file_glob", "expected_heartbeats"),
    [
        # Every member is evaluated, so both lines contribute matches.
        ("**/*", [(1, 1), (2, 2)]),
        # The first line belongs to a glob-rejected member: the lease must still
        # be refreshed, otherwise a archive of only rejected members runs dry.
        ("**/*.log", [(1, 0), (2, 1)]),
    ],
)
def test_line_processing_heartbeats_on_time_even_without_line_threshold(
    tmp_path: Path,
    file_glob: str,
    expected_heartbeats: list[tuple[int, int]],
) -> None:
    repo = FakeRepo()
    processor = make_processor(
        repo,
        FakeWriter(),
        tmp_path / "work",
        rules=make_rules(file_glob=file_glob),
        heartbeat_every_lines=10_000,
        heartbeat_every_seconds=5,
        clock=SteppingClock(step=6),
    )

    result = processor.process(
        make_task(
            tmp_path,
            members={"notes.txt": "ERROR one\n", "app.log": "ERROR two\n"},
        )
    )

    assert result.status == "succeeded"
    assert result.processed_lines == 2
    # Each 6s clock step passes the 5s window, so a heartbeat follows every line.
    assert repo.heartbeats == expected_heartbeats


def test_lease_lost_is_reraised_without_completing(tmp_path: Path) -> None:
    repo = FakeRepo()
    repo.heartbeat_result = False
    writer = FakeWriter()
    work_dir = tmp_path / "work"
    processor = make_processor(repo, writer, work_dir, batch_size=1000, heartbeat_every_lines=1)

    with pytest.raises(LeaseLost):
        processor.process(make_task(tmp_path))

    assert repo.completed == []
    assert not (work_dir / "10").exists()


def test_file_glob_only_evaluates_matching_members(tmp_path: Path) -> None:
    archive = write_zip(
        tmp_path / "mixed.zip",
        {
            "app.log": "ERROR root\n",
            "notes.txt": "ERROR notes\n",
            "nested/deep.log": "ERROR deep\n",
        },
    )
    repo = FakeRepo()
    writer = FakeWriter()
    task = ClaimedTask(
        id=40,
        job_id=20,
        source_path=archive,
        file_name="mixed.zip",
        attempts=1,
        rule_set_id=30,
        rule_version=1,
    )

    result = make_processor(
        repo, writer, tmp_path / "work", rules=make_rules(file_glob="**/*.log")
    ).process(task)

    assert result.status == "succeeded"
    assert result.processed_lines == 3
    assert result.matched_rows == 2
    assert [(m.member_path, m.rule_id) for m in writer.written] == [
        ("app.log", "error"),
        ("nested/deep.log", "error"),
    ]
    assert repo.completed == [(40, 1, "succeeded", 3, 2, None)]


@pytest.mark.parametrize(
    ("encoding", "fallback_encoding"),
    [("gb18030", "ascii"), ("utf-8", "gb18030")],
)
def test_ruleset_encoding_applies_when_task_has_no_override(
    tmp_path: Path, encoding: str, fallback_encoding: str
) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    task = make_task(
        tmp_path,
        members={"app.log": "错误 ERROR\n".encode("gb18030")},
    )
    rules = make_rules(encoding=encoding, fallback_encoding=fallback_encoding, keyword="错误")

    result = make_processor(repo, writer, tmp_path / "work", rules=rules).process(task)

    assert result.status == "succeeded"
    assert result.matched_rows == 1
    assert writer.written[0].raw_line == "错误 ERROR"


def test_task_encoding_override_wins_over_ruleset(tmp_path: Path) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    task = make_task(
        tmp_path,
        members={"app.log": "错误 ERROR\n".encode("gb18030")},
        encoding="gb18030",
        fallback_encoding="ascii",
    )
    rules = make_rules(encoding="utf-8", fallback_encoding="ascii", keyword="错误")

    result = make_processor(repo, writer, tmp_path / "work", rules=rules).process(task)

    assert result.status == "succeeded"
    assert result.matched_rows == 1
    assert writer.written[0].raw_line == "错误 ERROR"


def test_writer_receives_immutable_batch_snapshot(tmp_path: Path) -> None:
    repo = FakeRepo()
    writer = RetainingWriter()
    processor = make_processor(
        repo,
        writer,
        tmp_path / "work",
        batch_size=1,
        heartbeat_every_lines=1,
    )

    result = processor.process(make_task(tmp_path))

    assert result.status == "succeeded"
    assert result.matched_rows == 2
    assert len(writer.batches) == 2
    assert all(isinstance(batch, tuple) for batch in writer.batches)
    records = [record for batch in writer.batches for record in batch]
    assert [(record.rule_id, record.line_no) for record in records] == [
        ("error", 1),
        ("slow", 2),
    ]


def test_success_completion_failure_does_not_trigger_failed_completion(tmp_path: Path) -> None:
    repo = SuccessCompletionFailingRepo()
    writer = FakeWriter()
    work_dir = tmp_path / "work"

    with pytest.raises(RuntimeError, match="completion write failed"):
        make_processor(repo, writer, work_dir).process(make_task(tmp_path))

    assert [entry[2] for entry in repo.completed] == ["succeeded"]
    assert len(writer.written) == 2
    assert not (work_dir / "10").exists()


def test_write_fence_blocks_late_writes_of_a_reclaimed_task(tmp_path: Path) -> None:
    """A worker that already lost the lease may not write anything at all.

    Its rows would survive the new attempt's cleanup and show up as duplicates.
    """
    repo = FakeRepo()
    repo.touch_result = False
    writer = FakeWriter()
    processor = make_processor(repo, writer, tmp_path / "work")

    with pytest.raises(LeaseLost):
        processor.process(make_task(tmp_path, attempts=2))

    assert repo.touches == [(10, 2)]
    assert writer.deleted == []
    assert writer.written == []
    assert repo.completed == []
    assert not (tmp_path / "work" / "10").exists()


def test_each_batch_flush_is_preceded_by_a_lease_check(tmp_path: Path) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    processor = make_processor(
        repo,
        writer,
        tmp_path / "work",
        batch_size=1,
        heartbeat_every_lines=10_000,
        heartbeat_every_seconds=0,
        copy_heartbeat_seconds=1_000_000,
    )

    result = processor.process(make_task(tmp_path, members={"app.log": "ERROR a\nERROR b\n"}))

    assert result.status == "succeeded"
    assert writer.written
    # One fence before any write, then one per batch flush (batch_size=1).
    assert len(repo.touches) == len(writer.written) + 1


def test_cancelled_job_stops_processing_and_records_cancelled(tmp_path: Path) -> None:
    repo = FakeRepo()
    writer = FakeWriter()

    def cancel_check() -> bool:
        # 第一行之后立刻被取消：必须停止，不再评估后续行。
        return True

    processor = make_processor(
        repo,
        writer,
        tmp_path / "work",
        heartbeat_every_lines=1,
        cancel_check=cancel_check,
    )

    result = processor.process(
        make_task(tmp_path, members={"app.log": "ERROR a\nERROR b\nERROR c\n"})
    )

    assert result.status == "cancelled"
    assert repo.completed == [(10, 1, "cancelled", 1, 1, None)]
    assert result.processed_lines == 1


def test_missing_source_file_fails_permanently_without_retry(tmp_path: Path) -> None:
    repo = FakeRepo()
    processor = make_processor(repo, FakeWriter(), tmp_path / "work")
    task = make_task(tmp_path)
    task.source_path.unlink()

    result = processor.process(task)

    assert result.status == "failed"
    # 永久失败：写库标记不可重试，且不进入任何租约栅栏/写入。
    assert repo.completed == [(10, 1, "failed", 0, 0, result.error)]
    assert repo.retryables == [(10, False)]
    assert repo.touches == []


def test_size_change_is_permanent_failure(tmp_path: Path) -> None:
    repo = FakeRepo()
    processor = make_processor(repo, FakeWriter(), tmp_path / "work")
    task = make_task(tmp_path, expected_size_bytes=999_999)

    result = processor.process(task)

    assert result.status == "failed"
    assert "size" in (result.error or "").lower()
    assert repo.retryables == [(10, False)]
    assert repo.touches == []


def test_writable_mount_is_a_permanent_failure(tmp_path: Path, monkeypatch) -> None:
    """守卫先于任何复制生效：可写挂载上连租约栅栏都不该消耗。"""
    monkeypatch.setattr(source_guard, "detect_writability", lambda path: WRITABLE, raising=True)
    repo = FakeRepo()
    writer = FakeWriter()
    processor = make_processor(repo, writer, tmp_path / "work")

    result = processor.process(make_task(tmp_path))

    assert result.status == "failed"
    assert "writable" in (result.error or "").lower()
    assert repo.retryables == [(10, False)]
    assert repo.touches == []
    assert writer.deleted == []
    assert writer.written == []


def test_warn_policy_processes_the_task_on_a_writable_mount(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(source_guard, "detect_writability", lambda path: WRITABLE, raising=True)
    repo = FakeRepo()
    writer = FakeWriter()
    processor = make_processor(repo, writer, tmp_path / "work", nas_read_only_policy="warn")

    result = processor.process(make_task(tmp_path))

    assert result.status == "succeeded"
    assert repo.completed == [(10, 1, "succeeded", 2, 2, None)]


def test_guard_checks_the_directory_holding_the_archive(tmp_path: Path, monkeypatch) -> None:
    """嵌套挂载可以各自只读/可写，所以要按任务文件所在目录做最长前缀判定。"""
    seen: list[Path] = []

    def fake_detect(path: Path) -> str:
        seen.append(path)
        return UNKNOWN

    monkeypatch.setattr(source_guard, "detect_writability", fake_detect, raising=True)
    processor = make_processor(FakeRepo(), FakeWriter(), tmp_path / "work")

    assert processor.process(make_task(tmp_path)).status == "succeeded"
    assert seen == [tmp_path]


def test_naive_utc_mtime_from_database_is_accepted(tmp_path: Path) -> None:
    """The queue hands back mtime as naive UTC wall time; it must not look stale."""
    repo = FakeRepo()
    processor = make_processor(repo, FakeWriter(), tmp_path / "work")
    task = make_task(tmp_path)
    stat_mtime = int(task.source_path.stat().st_mtime)
    task = replace(
        task, expected_mtime=datetime.fromtimestamp(stat_mtime, tz=UTC).replace(tzinfo=None)
    )

    result = processor.process(task)

    assert result.status == "succeeded"
    # 成功收尾的 complete 也记录 retryable；关键是没有任何"不可重试"的失败写库。
    assert repo.completed == [(10, 1, "succeeded", 2, 2, None)]
    assert (10, False) not in repo.retryables


def test_real_mtime_change_is_still_a_permanent_failure(tmp_path: Path) -> None:
    repo = FakeRepo()
    processor = make_processor(repo, FakeWriter(), tmp_path / "work")
    task = make_task(tmp_path)
    stale = int(task.source_path.stat().st_mtime) - 3600
    task = replace(task, expected_mtime=datetime.fromtimestamp(stale, tz=UTC).replace(tzinfo=None))

    result = processor.process(task)

    assert result.status == "failed"
    assert "mtime" in (result.error or "").lower()
    assert repo.retryables == [(10, False)]


def test_cancel_check_follows_the_heartbeat_budget(tmp_path: Path) -> None:
    """规格 §7.3 是"每 2 万行查一次取消"，不是每行一次 SELECT。"""
    repo = FakeRepo()
    calls: list[int] = []

    def cancel_check() -> bool:
        calls.append(1)
        return False

    processor = make_processor(
        repo,
        FakeWriter(),
        tmp_path / "work",
        batch_size=10_000,
        heartbeat_every_lines=10,
        heartbeat_every_seconds=3_600,
        copy_heartbeat_seconds=3_600,
        cancel_check=cancel_check,
    )
    body = "".join(f"line {index}\n" for index in range(50))

    result = processor.process(make_task(tmp_path, members={"app.log": body}))

    assert result.status == "succeeded"
    assert result.processed_lines == 50
    # 50 行 / 每 10 行一次心跳 = 5 次取消检查，而不是 50 次。
    assert len(calls) == 5
