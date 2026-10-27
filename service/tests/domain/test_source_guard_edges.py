"""只读守卫的边界分支：挂载表解析退化、三态判定、非法策略。

挂载表路径在 Windows 上不可达（平台检查先行返回），所以涉及挂载表的用例
一律把 ``source_guard.sys.platform`` 伪装成 linux——与 test_source_guard.py
既有惯例一致。
"""

import pytest

from src.domain import source_guard
from src.domain.source_guard import (
    READ_ONLY,
    UNKNOWN,
    WRITABLE,
    detect_writability,
    guard_source,
    mount_options_for,
    read_mount_table,
    select_mount_options,
    writability_from_mode,
)


def test_read_mount_table_returns_none_when_all_candidates_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(source_guard.sys, "platform", "linux")
    monkeypatch.setattr(
        source_guard,
        "MOUNT_TABLE_CANDIDATES",
        (str(tmp_path / "no-such-mounts"), str(tmp_path / "no-such-mtab")),
    )
    # 全部候选 OSError -> continue 走到循环尽头，返回 None 而不是猜。
    assert read_mount_table() is None


def test_read_mount_table_falls_back_to_second_candidate(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(source_guard.sys, "platform", "linux")
    first = tmp_path / "mounts"
    second = tmp_path / "mtab"
    second.write_text("line-from-second\n", encoding="utf-8")
    monkeypatch.setattr(source_guard, "MOUNT_TABLE_CANDIDATES", (str(first), str(second)))
    assert read_mount_table() == ["line-from-second"]


def test_select_mount_options_skips_malformed_lines() -> None:
    lines = [
        "short line",
        "",  # split 后不足 4 段，跳过
        "/dev/sd1 /mnt/nas nfs4 ro,relatime 0 0",
    ]
    assert select_mount_options(lines, "/mnt/nas/data") == ["ro", "relatime"]


def test_mount_options_for_returns_none_when_resolve_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(source_guard.sys, "platform", "linux")
    readable = tmp_path / "mounts"
    readable.write_text("/dev/sd1 /mnt/nas nfs4 ro 0 0\n", encoding="utf-8")
    monkeypatch.setattr(source_guard, "MOUNT_TABLE_CANDIDATES", (str(readable),))

    real_resolve = type(tmp_path).resolve

    def broken_resolve(self, *args, **kwargs):
        raise OSError("stat storm")

    monkeypatch.setattr(type(tmp_path), "resolve", broken_resolve)
    # resolve 抛 OSError（挂载点抖动）时判不出来，返回 None 而不是猜。
    assert mount_options_for(tmp_path) is None
    monkeypatch.setattr(type(tmp_path), "resolve", real_resolve)


def test_detect_writability_maps_mount_options_to_three_states(monkeypatch, tmp_path) -> None:
    cases = {
        "ro": READ_ONLY,
        "rw": WRITABLE,
        # 只有既非 ro 也非 rw 的组合才降级 unknown，绝不猜。
        "nosuid": UNKNOWN,
    }
    for options, expected in cases.items():
        monkeypatch.setattr(source_guard, "mount_options_for", lambda path, o=options: o.split(","))
        assert detect_writability(tmp_path) == expected


def test_writability_from_mode_uses_write_bits() -> None:
    assert writability_from_mode(0o555) == READ_ONLY
    assert writability_from_mode(0o755) == UNKNOWN


def test_guard_source_rejects_unknown_policy(tmp_path) -> None:
    with pytest.raises(ValueError, match="nas_read_only_policy must be one of"):
        guard_source(tmp_path, policy="enforce-typo")


class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **fields: object) -> None:
        self.warnings.append((event, fields))


def test_guard_source_ignore_policy_stays_silent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(source_guard, "mount_options_for", lambda path: ["rw"])
    logger = RecordingLogger()
    # ignore 策略不告警也不抛（由运维显式选择承担风险）。
    assert guard_source(tmp_path, policy="ignore", logger=logger) == WRITABLE
    assert logger.warnings == []
