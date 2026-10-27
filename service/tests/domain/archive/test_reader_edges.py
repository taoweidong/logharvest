"""归档读取的边界分支：空后缀、损坏容器、预算成员错误、源大小退化。

`scan_archive` 返回的 `scan.lines` 是惰性生成器：容器级格式错误只在**消费**
行迭代时抛出（与 test_reader.py 既有用例一致，先 `list(scan.lines)` 再断言）。
"""

import zipfile
from pathlib import Path

import pytest

from src.domain.archive import ArchiveFormatError
from src.domain.archive import reader as reader_module
from src.domain.archive.reader import (
    ArchiveBudget,
    iter_archive_lines,
    normalize_suffixes,
    scan_archive,
)


def test_normalize_suffixes_skips_blank_entries() -> None:
    assert normalize_suffixes({"", "  ", "ZIP", ".gz"}) == {".zip", ".gz"}


def test_scan_archive_tolerates_unreadable_source_size(tmp_path, monkeypatch) -> None:
    """`_source_size_of` 取不到 stat 时按 0 处理：压缩比判定跳过，扫描照常完成。"""
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("app.log", "ERROR one\n")

    real_stat = Path.stat

    def broken_stat(self, *args, **kwargs):
        if self == archive:
            raise OSError("vanished mid-scan")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", broken_stat)
    scan = scan_archive(archive)
    assert list(scan.lines)
    assert scan.member_errors == []
    assert scan.succeeded_members == {"app.log"}


def test_invalid_gzip_single_file_raises_format_error(tmp_path) -> None:
    payload = tmp_path / "app.log.gz"
    payload.write_bytes(b"this is not gzip")
    scan = scan_archive(payload)
    with pytest.raises(ArchiveFormatError, match="invalid compressed file"):
        list(scan.lines)


def test_invalid_tar_file_raises_format_error(tmp_path) -> None:
    payload = tmp_path / "bundle.tar"
    payload.write_bytes(b"not a tar at all" * 8)
    scan = scan_archive(payload)
    with pytest.raises(ArchiveFormatError, match="invalid tar archive"):
        list(scan.lines)


def test_plain_text_file_with_exhausted_budget_reports_member_error(tmp_path) -> None:
    """单个文本源的预算超限不炸任务：记成员错误并停止读后续内容。"""
    payload = tmp_path / "app.log"
    payload.write_text("ERROR one\nERROR two\n", encoding="utf-8")
    budget = ArchiveBudget(max_total_bytes=4)
    scan = scan_archive(payload, budget=budget)
    list(scan.lines)
    assert scan.member_errors
    assert scan.member_errors[0].scan_incomplete is True


def test_zip_member_budget_exhaustion_is_member_scoped(tmp_path) -> None:
    """zip 内单成员解压超预算：该成员记错误后停止，绝不让 ScanLimitError 炸出。"""
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("big.log", "ERROR x\n" * 100)
        zf.writestr("small.log", "ERROR y\n")
    budget = ArchiveBudget(max_member_bytes=20)
    scan = scan_archive(archive, budget=budget)
    list(scan.lines)
    assert [error.member_path for error in scan.member_errors] == ["big.log"]
    # 预算停读不算 incomplete：成员是"干净地读到预算线"而不是中途损坏。
    assert scan.member_errors[0].scan_incomplete is False


def test_member_count_budget_bubbles_up_for_plain_source(tmp_path) -> None:
    """成员数预算在计费入口就拒绝（try 之外），ScanLimitError 必须向上抛。"""
    from src.domain.archive import ScanLimitError

    payload = tmp_path / "app.log"
    payload.write_text("ERROR one\n", encoding="utf-8")
    budget = ArchiveBudget(max_members=0)
    scan = scan_archive(payload, budget=budget)
    with pytest.raises(ScanLimitError, match="max_members=0"):
        list(scan.lines)


def test_line_read_budget_exhaustion_is_member_scoped_for_plain_source(tmp_path) -> None:
    """读行期间的总量预算超限被成员级捕获：记成员错误而不是炸出。"""
    payload = tmp_path / "app.log"
    payload.write_text("ERROR one\nERROR two\n", encoding="utf-8")
    budget = ArchiveBudget(max_total_bytes=4)
    scan = scan_archive(payload, budget=budget)
    list(scan.lines)
    assert [error.member_path for error in scan.member_errors] == ["app.log"]
    assert scan.member_errors[0].scan_incomplete is True


def test_reader_rethrows_permission_error_for_plain_source(tmp_path) -> None:
    """FileNotFoundError/PermissionError/IsADirectoryError 属于环境问题，必须上抛。"""
    payload = tmp_path / "no-such.log"
    with pytest.raises(FileNotFoundError):
        list(iter_archive_lines(payload, encoding="utf-8", fallback_encoding=None))


def test_reader_module_wraps_tarfile_open_errors(tmp_path, monkeypatch) -> None:
    """tarfile 打包阶段的一般 OSError 归一为 ArchiveFormatError（环境类错误除外）。"""
    payload = tmp_path / "bundle.tar"
    payload.write_bytes(b"x" * 512)

    def broken_open(*args, **kwargs):
        raise OSError("I/O error mid-read")

    monkeypatch.setattr(reader_module.tarfile, "open", broken_open)
    scan = scan_archive(payload)
    with pytest.raises(ArchiveFormatError, match="invalid tar archive"):
        list(scan.lines)
