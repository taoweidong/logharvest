import io
import tarfile
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

import src.application.worker.processor as processor_module
from src.application.worker.ports import ClaimedTask, MatchRecord
from src.application.worker.processor import LeaseLost, TaskProcessor
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
        self.touches: list[tuple[int, int]] = []
        self.completed: list[tuple[int, str, int, int, str | None]] = []
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
    ) -> None:
        self.completed.append((task_id, status, processed_lines, matched_rows, error))


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
    ) -> None:
        self.completed.append((task_id, status, processed_lines, matched_rows, error))
        if status == "succeeded":
            raise RuntimeError("completion write failed")


def write_zip(path: Path, members: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return path


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
    assert repo.completed == [(10, "succeeded", 2, 2, None)]
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
    real_iter = processor_module.iter_archive_lines

    def spy(path, encoding="utf-8", fallback_encoding="gb18030"):
        captured.append(Path(path))
        yield from real_iter(path, encoding=encoding, fallback_encoding=fallback_encoding)

    monkeypatch.setattr(processor_module, "iter_archive_lines", spy)

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
    assert repo.completed == [(10, "succeeded", 2, 2, None)]


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
    assert repo.completed == [(11, "failed", 0, 0, result.error)]
    assert writer.written == []
    assert not (work_dir / "11").exists()


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
    assert repo.completed[0][1] == "failed"
    assert writer.written == []
    assert not (work_dir / "10").exists()


def test_copy_refreshes_lease_with_touch(tmp_path: Path) -> None:
    repo = FakeRepo()
    writer = FakeWriter()
    processor = make_processor(
        repo,
        writer,
        tmp_path / "work",
        heartbeat_every_lines=10_000,
        copy_heartbeat_seconds=5,
        clock=SteppingClock(step=6),
    )

    result = processor.process(make_task(tmp_path))

    assert result.status == "succeeded"
    assert repo.touches == [(10, 1)]
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
    assert repo.completed == [(40, "succeeded", 3, 2, None)]


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

    assert [entry[1] for entry in repo.completed] == ["succeeded"]
    assert len(writer.written) == 2
    assert not (work_dir / "10").exists()
