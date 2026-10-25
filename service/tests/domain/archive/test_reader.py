import bz2
import gzip
import io
import logging
import lzma
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from src.domain.archive import ArchiveFormatError, ScanLimitError
from src.domain.archive import reader as reader_module
from src.domain.archive.reader import discover_archives, iter_archive_lines, scan_archive


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


def test_discover_archives_rejects_non_directory(tmp_path) -> None:
    path = tmp_path / "a.zip"
    path.write_bytes(b"x")

    with pytest.raises(NotADirectoryError):
        discover_archives(path)


def test_decode_replaces_undecodable_bytes_when_all_encodings_fail(tmp_path) -> None:
    path = tmp_path / "bad.log"
    path.write_bytes(b"\xff\n")

    records = list(iter_archive_lines(path))

    assert records[0].text == "\ufffd"


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


def test_discover_archives_excludes_raw_files_by_default(tmp_path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "raw.log").write_text("x")
    (root / "raw.txt").write_text("x")
    (root / "a.zip").write_bytes(b"zip")
    (root / "b.log.gz").write_bytes(b"gz")

    assert [path.name for path in discover_archives(root)] == ["a.zip", "b.log.gz"]
    assert [path.name for path in discover_archives(root, suffixes={".log"})] == ["raw.log"]


def test_discover_archives_normalizes_explicit_suffixes(tmp_path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "raw.log").write_text("x")
    (root / "raw.txt").write_text("x")
    (root / "a.zip").write_bytes(b"zip")

    assert [path.name for path in discover_archives(root, suffixes={".LOG", "TXT"})] == [
        "raw.log",
        "raw.txt",
    ]
    assert discover_archives(root, suffixes=set()) == []


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


def test_zip_symlink_members_are_skipped(tmp_path) -> None:
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        link = zipfile.ZipInfo("link.log")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link, "real.log")
        zf.writestr("real.log", "line\n")

    records = list(iter_archive_lines(archive))
    assert [(r.member_path, r.text) for r in records] == [("real.log", "line")]


@pytest.mark.parametrize(
    ("name", "mode"),
    [("plain.tar", "w"), ("plain.tgz", "w:gz"), ("plain.tar.gz", "w:gz")],
)
def test_tar_names_are_streamed(tmp_path, name, mode) -> None:
    archive = tmp_path / name
    source = tmp_path / "app.log"
    source.write_text("line1\nline2\n", encoding="utf-8")
    with tarfile.open(archive, mode) as tf:
        tf.add(source, arcname="nested/app.log")

    records = list(iter_archive_lines(archive))
    assert [(r.member_path, r.line_no) for r in records] == [
        ("nested/app.log", 1),
        ("nested/app.log", 2),
    ]


@pytest.mark.parametrize("name", ["broken.tar", "broken.tgz", "broken.tar.gz"])
def test_corrupt_tar_named_files_raise_without_records(tmp_path, name) -> None:
    path = tmp_path / name
    path.write_text("not a tar archive\nsecond line\n", encoding="utf-8")

    records: list = []
    with pytest.raises(ArchiveFormatError, match="invalid tar archive"):
        records = list(iter_archive_lines(path))
    assert records == []


def test_gzip_text_payload_named_tar_gz_is_not_decoded(tmp_path) -> None:
    path = tmp_path / "text-payload.tar.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write("plain text\nsecond line\n")

    records: list = []
    with pytest.raises(ArchiveFormatError, match="invalid tar archive"):
        records = list(iter_archive_lines(path))
    assert records == []


def test_corrupt_zip_raises_without_records(tmp_path) -> None:
    path = tmp_path / "broken.zip"
    path.write_text("not a zip\n", encoding="utf-8")

    records: list = []
    with pytest.raises(ArchiveFormatError, match="invalid zip archive"):
        records = list(iter_archive_lines(path))
    assert records == []


def test_scan_archive_continues_after_corrupt_zip_member(tmp_path) -> None:
    archive = tmp_path / "partial.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bad.log", "bad line\n" * 100)
        zf.writestr("good.log", "good line\n")
    corrupt_zip_member(archive, "bad.log")

    scan = scan_archive(archive)
    records = list(scan.lines)

    assert [(record.member_path, record.text) for record in records] == [
        ("good.log", "good line")
    ]
    assert scan.succeeded_members == {"good.log"}
    assert [error.member_path for error in scan.member_errors] == ["bad.log"]
    assert "decompress" in scan.member_errors[0].message


def test_scan_archive_stops_after_corrupt_tar_member(tmp_path) -> None:
    archive = tmp_path / "partial.tar"
    with tarfile.open(archive, "w") as tf:
        for name, text in (("good.log", "good line\n"), ("bad.log", "bad line\n" * 100)):
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    truncate_tar_member(archive, "bad.log")

    scan = scan_archive(archive)
    records = list(scan.lines)

    assert [(record.member_path, record.text) for record in records] == [
        ("good.log", "good line")
    ]
    assert scan.succeeded_members == {"good.log"}
    assert [error.member_path for error in scan.member_errors] == ["bad.log"]


def test_scan_archive_reports_corrupt_tar_header(tmp_path) -> None:
    archive = tmp_path / "corrupt-header.tar"
    with tarfile.open(archive, "w") as tf:
        for name, text in (("good.log", "good line\n"), ("bad.log", "bad line\n")):
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    with tarfile.open(archive) as tf:
        second_header = tf.getmember("bad.log").offset
    payload = bytearray(archive.read_bytes())
    payload[second_header + 10] ^= 0xFF
    archive.write_bytes(payload)

    scan = scan_archive(archive)
    records = list(scan.lines)

    assert [(record.member_path, record.text) for record in records] == [
        ("good.log", "good line")
    ]
    assert scan.succeeded_members == {"good.log"}
    assert len(scan.member_errors) == 1
    assert scan.member_errors[0].member_path is None
    assert scan.member_errors[0].scan_incomplete is True


def test_scan_archive_reports_tar_truncated_at_member_boundary(tmp_path) -> None:
    archive = tmp_path / "truncated.tar"
    with tarfile.open(archive, "w") as tf:
        for name, text in (("good.log", "good line\n"), ("bad.log", "bad line\n")):
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    with tarfile.open(archive) as tf:
        first_end = tf.getmember("good.log").offset_data + 512
    archive.write_bytes(archive.read_bytes()[:first_end])

    scan = scan_archive(archive)
    records = list(scan.lines)

    assert [(record.member_path, record.text) for record in records] == [
        ("good.log", "good line")
    ]
    assert scan.succeeded_members == {"good.log"}
    assert len(scan.member_errors) == 1
    assert scan.member_errors[0].member_path is None
    assert scan.member_errors[0].scan_incomplete is True


def test_corrupt_single_gzip_raises_archive_format_error(tmp_path) -> None:
    path = tmp_path / "bad.log.gz"
    path.write_bytes(b"not gzip")

    scan = scan_archive(path)
    with pytest.raises(ArchiveFormatError, match="invalid compressed file"):
        list(scan.lines)


def test_iter_archive_lines_stays_strict_after_member_error(tmp_path) -> None:
    archive = tmp_path / "partial.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bad.log", "bad line\n" * 100)
        zf.writestr("good.log", "good line\n")
    corrupt_zip_member(archive, "bad.log")

    with pytest.raises(ArchiveFormatError, match=r"bad\.log"):
        list(iter_archive_lines(archive))


@pytest.mark.parametrize(
    ("name", "mode"),
    [("empty.zip", None), ("empty.tar", "w"), ("empty.tar.gz", "w:gz"), ("empty.tgz", "w:gz")],
)
def test_empty_archive_yields_no_records(tmp_path, name, mode) -> None:
    path = tmp_path / name
    if mode is None:
        with zipfile.ZipFile(path, "w"):
            pass
    else:
        with tarfile.open(path, mode):
            pass

    assert list(iter_archive_lines(path)) == []


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


def test_crlf_lines_are_stripped(tmp_path) -> None:
    path = tmp_path / "crlf.log"
    path.write_bytes(b"alpha\r\nbeta\r\n\r\n")

    assert [record.text for record in iter_archive_lines(path)] == ["alpha", "beta", ""]


def test_exact_cap_crlf_line_is_not_truncated(tmp_path) -> None:
    path = tmp_path / "exact.log"
    path.write_bytes(b"x" * 10 + b"\r\nnext\r\n")

    records = list(iter_archive_lines(path, max_line_bytes=10))
    assert [(record.text, record.truncated) for record in records] == [
        ("x" * 10, False),
        ("next", False),
    ]


def test_over_cap_crlf_line_is_truncated_and_resynced(tmp_path) -> None:
    path = tmp_path / "over.log"
    path.write_bytes(b"y" * 11 + b"\r\nnext\r\n")

    records = list(iter_archive_lines(path, max_line_bytes=10))
    assert [(record.text, record.truncated) for record in records] == [
        ("y" * 10, True),
        ("next", False),
    ]


def test_discover_archives_respects_max_depth(tmp_path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "top.zip").write_bytes(b"zip")
    for depth in range(1, 7):
        directory = root.joinpath(*(f"d{level}" for level in range(1, depth + 1)))
        directory.mkdir(parents=True)
        (directory / f"level{depth}.zip").write_bytes(b"zip")

    assert [path.name for path in discover_archives(root)] == [
        "top.zip",
        "level1.zip",
        "level2.zip",
        "level3.zip",
        "level4.zip",
        "level5.zip",
    ]
    assert [path.name for path in discover_archives(root, max_depth=2)] == [
        "top.zip",
        "level1.zip",
        "level2.zip",
    ]


def test_discover_archives_skips_symlinked_dirs_and_files(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.zip").write_bytes(b"zip")

    root = tmp_path / "logs"
    root.mkdir()
    (root / "real.zip").write_bytes(b"zip")
    try:
        os.symlink(outside, root / "linked-dir", target_is_directory=True)
        os.symlink(outside / "leak.zip", root / "linked-file.zip")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    assert [path.name for path in discover_archives(root)] == ["real.zip"]


def test_discover_archives_non_recursive(tmp_path) -> None:
    root = tmp_path / "logs"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "a.zip").write_bytes(b"zip")
    (root / "b.zip").write_bytes(b"zip")
    (root / "c.log").write_text("x")

    assert [path.name for path in discover_archives(root, recursive=False)] == ["b.zip"]
    assert [path.name for path in discover_archives(root, recursive=False, suffixes={".log"})] == [
        "c.log"
    ]


def test_discover_archives_unreadable_root_raises(tmp_path, monkeypatch) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    real_scandir = os.scandir

    def fake_scandir(path):
        if Path(path) == root:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    with pytest.raises(PermissionError):
        discover_archives(root)


def test_discover_archives_warns_on_unreadable_subdirectory(tmp_path, monkeypatch, caplog) -> None:
    root = tmp_path / "logs"
    blocked = root / "blocked"
    blocked.mkdir(parents=True)
    (root / "ok.zip").write_bytes(b"zip")
    real_scandir = os.scandir

    def fake_scandir(path):
        if Path(path) == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    with caplog.at_level(logging.WARNING, logger="src.domain.archive.reader"):
        found = discover_archives(root)
    assert [path.name for path in found] == ["ok.zip"]
    assert "blocked" in caplog.text


@pytest.mark.parametrize(("suffix", "opener"), [(".bz2", bz2.open), (".xz", lzma.open)])
def test_single_bz2_and_xz_are_streamed(tmp_path, suffix, opener) -> None:
    path = tmp_path / f"app.log{suffix}"
    with opener(path, "wb") as stream:
        stream.write("第一行\nsecond\n".encode())

    records = list(iter_archive_lines(path))
    assert [(r.member_path, r.file_name, r.line_no, r.text) for r in records] == [
        ("app.log", "app.log", 1, "第一行"),
        ("app.log", "app.log", 2, "second"),
    ]


@pytest.mark.parametrize(("suffix", "mode"), [(".tar.bz2", "w:bz2"), (".tar.xz", "w:xz")])
def test_tar_compressed_members_reset_line_numbers(tmp_path, suffix, mode) -> None:
    archive = tmp_path / f"a{suffix}"
    first = tmp_path / "first.log"
    first.write_text("a\nb\n", encoding="utf-8")
    second = tmp_path / "second.log"
    second.write_text("c\n", encoding="utf-8")
    with tarfile.open(archive, mode) as tf:
        tf.add(first, arcname="first.log")
        tf.add(second, arcname="nested/second.log")

    records = list(iter_archive_lines(archive))
    assert [(r.member_path, r.line_no, r.text) for r in records] == [
        ("first.log", 1, "a"),
        ("first.log", 2, "b"),
        ("nested/second.log", 1, "c"),
    ]


def test_unsupported_suffix_raises_value_error(tmp_path) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(b"data")

    with pytest.raises(ValueError, match="unsupported archive type"):
        list(iter_archive_lines(path))


def test_discover_archives_recursive_requires_regular_files(tmp_path, monkeypatch) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "real.zip").write_bytes(b"zip")
    (root / "pipe.zip").write_bytes(b"zip")
    real_is_file = Path.is_file

    def fake_is_file(self) -> bool:
        if self.name == "pipe.zip":
            return False
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", fake_is_file)

    assert [path.name for path in discover_archives(root)] == ["real.zip"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are not available on this platform")
def test_discover_archives_recursive_skips_fifos(tmp_path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "real.zip").write_bytes(b"zip")
    os.mkfifo(root / "pipe.zip")

    assert [path.name for path in discover_archives(root)] == ["real.zip"]


def test_discover_archives_max_files_raises_early(tmp_path, monkeypatch) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    for index in range(10):
        (root / f"{index:02d}.zip").write_bytes(b"zip")

    assert len(discover_archives(root, max_files=10)) == 10

    matched: list[str] = []
    real_matches = reader_module._matches_suffix

    def counting_matches(path: Path, allowed) -> bool:
        matched.append(path.name)
        return real_matches(path, allowed)

    monkeypatch.setattr(reader_module, "_matches_suffix", counting_matches)

    with pytest.raises(ScanLimitError) as exc:
        discover_archives(root, max_files=1)
    assert exc.value.reason == "max_files"
    # Stops as soon as the cap is exceeded instead of walking the whole tree.
    assert matched == ["00.zip", "01.zip"]


def test_discover_archives_deadline_raises(tmp_path, monkeypatch) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "a.zip").write_bytes(b"zip")

    clock = iter([100.0, 100.5, 120.0])
    monkeypatch.setattr(reader_module.time, "monotonic", lambda: next(clock, 120.0))

    with pytest.raises(ScanLimitError) as exc:
        discover_archives(root, deadline_seconds=10)

    assert exc.value.reason == "deadline"


def test_discover_archives_deadline_not_reached(tmp_path, monkeypatch) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "a.zip").write_bytes(b"zip")

    monkeypatch.setattr(reader_module.time, "monotonic", lambda: 100.0)

    assert [path.name for path in discover_archives(root, deadline_seconds=10)] == ["a.zip"]


def test_discover_archives_rejects_negative_max_files(tmp_path) -> None:
    root = tmp_path / "logs"
    root.mkdir()

    with pytest.raises(ValueError, match="max_files"):
        discover_archives(root, max_files=-1)
