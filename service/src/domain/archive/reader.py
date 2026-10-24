import bz2
import gzip
import lzma
import os
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

from src.domain.archive.models import LineRecord

ARCHIVE_SUFFIXES = {
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz", ".txz", ".log", ".txt",
}


def discover_archives(
    source_path: Path,
    recursive: bool = True,
    max_depth: int = 5,
    suffixes: set[str] | None = None,
) -> list[Path]:
    root = Path(source_path)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    allowed = suffixes or ARCHIVE_SUFFIXES
    found: list[Path] = []

    if not recursive:
        for path in sorted(root.iterdir()):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in allowed:
                found.append(path)
        return found

    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".") and not (current_path / name).is_symlink()
        )
        if depth >= max_depth:
            dirnames[:] = []
        for name in sorted(filenames):
            path = current_path / name
            if path.is_symlink() or path.suffix.lower() not in allowed:
                continue
            found.append(path)
    return found


def _decode(raw: bytes, encoding: str, fallback_encoding: str) -> str:
    for candidate in (encoding, fallback_encoding):
        try:
            return raw.decode(candidate)
        except UnicodeDecodeError:
            continue
    return raw.decode(encoding, errors="replace")


def _iter_binary_lines(
    stream,
    member_path: str,
    file_name: str,
    encoding: str,
    fallback_encoding: str,
    max_line_bytes: int,
) -> Iterator[LineRecord]:
    line_no = 0
    while True:
        raw = stream.readline(max_line_bytes + 1)
        if not raw:
            return
        truncated = False
        if len(raw) > max_line_bytes and not raw.endswith(b"\n"):
            raw = raw[:max_line_bytes]
            truncated = True
            while True:
                rest = stream.readline(8192)
                if not rest or rest.endswith(b"\n"):
                    break
        line_no += 1
        yield LineRecord(
            line_no=line_no,
            text=_decode(raw.rstrip(b"\r\n"), encoding, fallback_encoding),
            member_path=member_path,
            file_name=file_name,
            truncated=truncated,
        )


def _iter_zip(path: Path, encoding: str, fallback_encoding: str, max_line_bytes: int):
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            with archive.open(info) as stream:
                yield from _iter_binary_lines(
                    stream,
                    info.filename,
                    Path(info.filename).name,
                    encoding,
                    fallback_encoding,
                    max_line_bytes,
                )


def _iter_tar_or_single(path: Path, encoding: str, fallback_encoding: str, max_line_bytes: int):
    try:
        with tarfile.open(path, mode="r|*") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                with stream:
                    yield from _iter_binary_lines(
                        stream,
                        member.name,
                        Path(member.name).name,
                        encoding,
                        fallback_encoding,
                        max_line_bytes,
                    )
        return
    except tarfile.ReadError:
        pass

    opener = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}[path.suffix.lower()]
    with opener(path, "rb") as stream:
        yield from _iter_binary_lines(
            stream,
            path.stem,
            path.stem,
            encoding,
            fallback_encoding,
            max_line_bytes,
        )


def iter_archive_lines(
    path: Path,
    encoding: str = "utf-8",
    fallback_encoding: str = "gb18030",
    max_line_bytes: int = 1_048_576,
) -> Iterator[LineRecord]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".zip":
        yield from _iter_zip(path, encoding, fallback_encoding, max_line_bytes)
    elif suffix in {".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz", ".txz"}:
        yield from _iter_tar_or_single(path, encoding, fallback_encoding, max_line_bytes)
    elif suffix in {".log", ".txt"}:
        with path.open("rb") as stream:
            yield from _iter_binary_lines(
                stream,
                path.name,
                path.name,
                encoding,
                fallback_encoding,
                max_line_bytes,
            )
    else:
        raise ValueError(f"unsupported archive type: {path}")
