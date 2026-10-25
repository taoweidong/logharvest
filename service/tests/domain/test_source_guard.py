"""只读判定的两条硬要求：只用无副作用证据，判不出来就如实报 unknown。

写探针（在 NAS 上建临时文件再删）本身就违反只读不变量，且清理失败会在用户的
源数据上留残渣，所以这里一律拒绝；可用的正向证据只有 POSIX 挂载表选项与
``st_mode`` 写位，而 ``unknown`` 永远不能被降级成"已确认只读"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.domain import source_guard
from src.domain.source_guard import (
    READ_ONLY,
    UNKNOWN,
    WRITABLE,
    SourceNotReadOnly,
    detect_writability,
    guard_source,
    mode_writability,
    mount_options_for,
    read_mount_table,
    select_mount_options,
    writability_from_mode,
)

MOUNTS = [
    "/dev/sda1 / ext4 rw,relatime 0 0",
    "/dev/sdb1 /nas cifs ro,uid=1000 0 0",
    "/dev/sdc1 /nas/deep nfs4 rw 0 0",
]


class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **fields: object) -> None:
        self.warnings.append((event, fields))


def test_select_mount_options_prefers_longest_prefix() -> None:
    assert select_mount_options(MOUNTS, "/nas/deep/logs") == ["rw"]
    assert select_mount_options(MOUNTS, "/nas/other") == ["ro", "uid=1000"]
    assert select_mount_options(MOUNTS, "/elsewhere") == ["rw", "relatime"]
    # 最长前缀优先，而不是"第一条命中就行"：行的顺序不能改变结论。
    assert select_mount_options(list(reversed(MOUNTS)), "/nas/deep/logs") == ["rw"]
    assert select_mount_options([], "/nas") is None


def test_select_mount_options_never_matches_across_a_path_boundary() -> None:
    assert select_mount_options(["/dev/sdb1 /nas cifs ro 0 0"], "/nashost/logs") is None


def test_select_mount_options_unescapes_octal_separator() -> None:
    lines = [r"/dev/sdb1 /nas\040share cifs ro 0 0"]
    assert select_mount_options(lines, "/nas share/logs") == ["ro"]


def test_mount_options_for_matches_a_real_resolved_path(tmp_path: Path, monkeypatch) -> None:
    mount = str(tmp_path.resolve()).replace(" ", "\\040")
    monkeypatch.setattr(
        source_guard, "read_mount_table", lambda: [f"/dev/sdb1 {mount} cifs ro 0 0"]
    )

    assert mount_options_for(tmp_path / "sub") == ["ro"]
    assert detect_writability(tmp_path / "sub") == READ_ONLY


def test_mount_options_for_is_none_without_a_mount_table(tmp_path: Path, monkeypatch) -> None:
    # Windows/SMB 上没有 /proc/mounts：必须返回 None，而不是替用户猜一个结论。
    monkeypatch.setattr(source_guard, "read_mount_table", lambda: None)
    assert mount_options_for(tmp_path) is None


def test_read_mount_table_returns_none_off_posix(monkeypatch) -> None:
    monkeypatch.setattr(source_guard.sys, "platform", "win32")
    assert read_mount_table() is None


def test_read_mount_table_reads_the_first_readable_candidate(tmp_path: Path, monkeypatch) -> None:
    table = tmp_path / "mounts"
    table.write_text("dev /nas cifs ro 0 0\n", encoding="utf-8")
    monkeypatch.setattr(source_guard.sys, "platform", "linux")
    monkeypatch.setattr(
        source_guard,
        "MOUNT_TABLE_CANDIDATES",
        (str(tmp_path / "missing"), str(table)),
    )
    assert read_mount_table() == ["dev /nas cifs ro 0 0"]


def test_writability_from_mode_only_reads_write_bits() -> None:
    assert writability_from_mode(0o040555) == READ_ONLY
    assert writability_from_mode(0o100444) == READ_ONLY
    # 有写位只说明"我们这边看不能写"，不等于挂载可写，所以是 unknown 而非 writable。
    assert writability_from_mode(0o040755) == UNKNOWN
    assert writability_from_mode(0o100644) == UNKNOWN


def test_mode_writability_never_returns_writable(tmp_path: Path) -> None:
    assert mode_writability(tmp_path) in {READ_ONLY, UNKNOWN}


def test_mode_writability_of_missing_path_is_unknown(tmp_path: Path) -> None:
    assert mode_writability(tmp_path / "nope") == UNKNOWN


def test_detect_writability_falls_back_to_mode_without_mount_table(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(source_guard, "read_mount_table", lambda: None)
    monkeypatch.setattr(source_guard, "mode_writability", lambda path: UNKNOWN)
    assert detect_writability(tmp_path) == UNKNOWN

    monkeypatch.setattr(source_guard, "mode_writability", lambda path: READ_ONLY)
    assert detect_writability(tmp_path) == READ_ONLY


def test_detect_writability_is_unknown_for_ambiguous_mount_options(
    tmp_path: Path, monkeypatch
) -> None:
    # 既没写 ro 也没写 rw 的挂载（例如 bind/autofs）不足以断言任何结论。
    monkeypatch.setattr(
        source_guard,
        "read_mount_table",
        lambda: ["/dev/sda1 / ext4 noatime,uid=0 0 0"],
    )
    assert detect_writability(tmp_path) == UNKNOWN


def test_detection_leaves_no_trace_on_the_source(tmp_path: Path) -> None:
    """判定不得在数据源上创建或改动任何东西。"""
    archive = tmp_path / "a.zip"
    archive.write_bytes(b"stale")
    listing = sorted((str(p), p.stat().st_mtime_ns, p.stat().st_mode) for p in tmp_path.iterdir())

    detect_writability(tmp_path)

    assert (
        sorted((str(p), p.stat().st_mtime_ns, p.stat().st_mode) for p in tmp_path.iterdir())
        == listing
    )
    assert archive.read_bytes() == b"stale"


def test_guard_accepts_confirmed_read_only_under_enforce(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(source_guard, "detect_writability", lambda path: READ_ONLY)
    assert guard_source(tmp_path, policy="enforce") == READ_ONLY


def test_guard_refuses_writable_under_enforce(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(source_guard, "detect_writability", lambda path: WRITABLE)

    with pytest.raises(SourceNotReadOnly, match="writable"):
        guard_source(tmp_path, policy="enforce")

    # warn / ignore 不阻断，但判定值必须原样返回，供上层记录与展示。
    assert guard_source(tmp_path, policy="warn") == WRITABLE
    assert guard_source(tmp_path, policy="ignore") == WRITABLE


def test_guard_does_not_refuse_unknown_under_enforce(tmp_path: Path, monkeypatch) -> None:
    """unknown 不是"可写"的证据：不能因为判不出来就拒绝，也不能声称只读。"""
    monkeypatch.setattr(source_guard, "detect_writability", lambda path: UNKNOWN)
    assert guard_source(tmp_path, policy="enforce") == UNKNOWN


def test_guard_warns_when_read_only_cannot_be_confirmed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(source_guard, "detect_writability", lambda path: UNKNOWN)
    logger = RecordingLogger()

    assert guard_source(tmp_path, policy="warn", logger=logger) == UNKNOWN
    assert logger.warnings == [
        (
            "nas_read_only_not_confirmed",
            {"path": str(tmp_path), "verdict": UNKNOWN, "policy": "warn"},
        )
    ]


def test_guard_logs_the_refusal_before_raising(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(source_guard, "detect_writability", lambda path: WRITABLE)
    logger = RecordingLogger()

    with pytest.raises(SourceNotReadOnly):
        guard_source(tmp_path, policy="enforce", logger=logger)

    assert [event for event, _ in logger.warnings] == ["nas_read_only_not_confirmed"]


def test_guard_stays_silent_when_policy_is_ignore(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(source_guard, "detect_writability", lambda path: WRITABLE)
    logger = RecordingLogger()

    assert guard_source(tmp_path, policy="ignore", logger=logger) == WRITABLE
    assert logger.warnings == []


def test_guard_does_not_log_a_confirmed_read_only_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(source_guard, "detect_writability", lambda path: READ_ONLY)
    logger = RecordingLogger()

    guard_source(tmp_path, policy="enforce", logger=logger)
    assert logger.warnings == []
