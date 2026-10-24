import bz2
import gzip
import logging
import lzma
import os
import stat
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

from src.domain.archive.models import ArchiveFormatError, LineRecord

logger = logging.getLogger(__name__)

TAR_SUFFIXES = frozenset(
    {".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz"}
)
SINGLE_COMPRESSED_SUFFIXES = frozenset({".gz", ".bz2", ".xz"})
RAW_SUFFIXES = frozenset({".log", ".txt"})
ARCHIVE_SUFFIXES = frozenset({".zip"}) | TAR_SUFFIXES | SINGLE_COMPRESSED_SUFFIXES

_SINGLE_OPENERS = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}


def _normalize_suffixes(suffixes: set[str]) -> set[str]:
    normalized: set[str] = set()
    for suffix in suffixes:
        text = suffix.strip().lower()
        if not text:
            continue
        normalized.add(text if text.startswith(".") else f".{text}")
    return normalized


def _matches_suffix(path: Path, allowed: set[str] | frozenset[str]) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in allowed)


def _ensure_readable_directory(root: Path) -> None:
    # os.scandir validates open/read permission on the root before os.walk
    # would otherwise swallow the error and return an empty result.
    with os.scandir(root) as entries:
        for _ in entries:
            break


def discover_archives(
    source_path: Path,
    recursive: bool = True,
    max_depth: int = 5,
    suffixes: set[str] | None = None,
) -> list[Path]:
    root = Path(source_path)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    _ensure_readable_directory(root)

    allowed = ARCHIVE_SUFFIXES if suffixes is None else _normalize_suffixes(suffixes)
    found: list[Path] = []

    if not recursive:
        for path in sorted(root.iterdir()):
            if path.is_file() and not path.is_symlink() and _matches_suffix(path, allowed):
                found.append(path)
        return found

    def on_walk_error(error: OSError) -> None:
        logger.warning("cannot read directory %s: %s", error.filename, error)

    for current, dirnames, filenames in os.walk(root, followlinks=False, onerror=on_walk_error):
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
            if path.is_symlink() or not path.is_file() or not _matches_suffix(path, allowed):
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
        # Read room for a trailing CRLF so an exact-cap line is not truncated.
        raw = stream.readline(max_line_bytes + 2)
        if not raw:
            return
        content = raw.rstrip(b"\r\n")
        truncated = len(content) > max_line_bytes
        if truncated:
            content = content[:max_line_bytes]
        if not raw.endswith(b"\n"):
            # The bounded read stopped before the line ended (or the line is
            # exactly at EOF); skip the rest so the next record is a real line.
            while True:
                rest = stream.readline(8192)
                if not rest or rest.endswith(b"\n"):
                    break
        line_no += 1
        yield LineRecord(
            line_no=line_no,
            text=_decode(content, encoding, fallback_encoding),
            member_path=member_path,
            file_name=file_name,
            truncated=truncated,
        )


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _iter_zip(
    path: Path,
    encoding: str,
    fallback_encoding: str,
    max_line_bytes: int,
) -> Iterator[LineRecord]:
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir() or _is_zip_symlink(info):
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
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ArchiveFormatError(f"invalid zip archive: {path}") from exc


def _is_tar_name(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in TAR_SUFFIXES)


def _iter_tar(
    path: Path,
    encoding: str,
    fallback_encoding: str,
    max_line_bytes: int,
) -> Iterator[LineRecord]:
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
    except (tarfile.TarError, EOFError, lzma.LZMAError, gzip.BadGzipFile) as exc:
        raise ArchiveFormatError(f"invalid tar archive: {path}") from exc
    except OSError as exc:
        if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError)):
            raise
        raise ArchiveFormatError(f"invalid tar archive: {path}") from exc


def _iter_single_compressed(
    path: Path,
    encoding: str,
    fallback_encoding: str,
    max_line_bytes: int,
) -> Iterator[LineRecord]:
    opener = _SINGLE_OPENERS[path.suffix.lower()]
    try:
        with opener(path, "rb") as stream:
            yield from _iter_binary_lines(
                stream,
                path.stem,
                path.stem,
                encoding,
                fallback_encoding,
                max_line_bytes,
            )
    except (EOFError, lzma.LZMAError, gzip.BadGzipFile) as exc:
        raise ArchiveFormatError(f"invalid compressed file: {path}") from exc
    except OSError as exc:
        if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError)):
            raise
        raise ArchiveFormatError(f"invalid compressed file: {path}") from exc


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
    elif _is_tar_name(path.name):
        # The name declares a tar container; never fall back to decoding the
        # bytes as a single compressed log stream.
        yield from _iter_tar(path, encoding, fallback_encoding, max_line_bytes)
    elif suffix in SINGLE_COMPRESSED_SUFFIXES:
        yield from _iter_single_compressed(path, encoding, fallback_encoding, max_line_bytes)
    elif suffix in RAW_SUFFIXES:
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
