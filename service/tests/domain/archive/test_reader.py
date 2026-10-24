import bz2
import gzip
import lzma
import os
import tarfile
import zipfile

import pytest

from src.domain.archive.reader import discover_archives, iter_archive_lines


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


def test_tar_gz_member_is_streamed(tmp_path) -> None:
    archive = tmp_path / "a.tar.gz"
    source = tmp_path / "app.log"
    source.write_text("line1\nline2\n")
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source, arcname="nested/app.log")

    records = list(iter_archive_lines(archive))
    assert [(r.member_path, r.line_no) for r in records] == [
        ("nested/app.log", 1),
        ("nested/app.log", 2),
    ]


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


def test_discover_archives_respects_max_depth(tmp_path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "top.log").write_text("x")
    for depth in range(1, 7):
        directory = root.joinpath(*(f"d{level}" for level in range(1, depth + 1)))
        directory.mkdir(parents=True)
        (directory / f"level{depth}.log").write_text("x")

    assert [path.name for path in discover_archives(root)] == [
        "top.log",
        "level1.log",
        "level2.log",
        "level3.log",
        "level4.log",
        "level5.log",
    ]
    assert [path.name for path in discover_archives(root, max_depth=2)] == [
        "top.log",
        "level1.log",
        "level2.log",
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
    (root / "b.log").write_text("x")

    assert [path.name for path in discover_archives(root, recursive=False)] == ["b.log"]


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
