import bz2
import gzip
import logging
import lzma
import os
import stat
import tarfile
import time
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from src.domain.archive.models import ArchiveFormatError, LineRecord, ScanLimitError

logger = logging.getLogger(__name__)

TAR_SUFFIXES = frozenset({".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz"})
SINGLE_COMPRESSED_SUFFIXES = frozenset({".gz", ".bz2", ".xz"})
RAW_SUFFIXES = frozenset({".log", ".txt"})
ARCHIVE_SUFFIXES = frozenset({".zip"}) | TAR_SUFFIXES | SINGLE_COMPRESSED_SUFFIXES

_SINGLE_OPENERS = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}
_TAR_ERRORS = (tarfile.TarError, EOFError, lzma.LZMAError, gzip.BadGzipFile)
_ZIP_MEMBER_ERRORS = (
    zipfile.BadZipFile,
    RuntimeError,
    NotImplementedError,
    EOFError,
    zlib.error,
)


@dataclass(frozen=True)
class ArchiveMemberError:
    """A member that could not be read; ``None`` means the container itself.

    ``scan_incomplete`` marks an error after which remaining members could not
    be examined (streaming tar), so the caller must not dismiss it via a
    member-path filter.
    """

    member_path: str | None
    message: str
    scan_incomplete: bool = False


@dataclass
class ArchiveScan:
    """Line stream plus member-level outcomes observed while consuming it."""

    lines: Iterator[LineRecord]
    succeeded_members: set[str] = field(default_factory=set)
    member_errors: list[ArchiveMemberError] = field(default_factory=list)


def normalize_suffixes(suffixes: set[str]) -> set[str]:
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
    max_files: int | None = None,
    deadline_seconds: float | None = None,
) -> list[Path]:
    root = Path(source_path)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    if max_files is not None and max_files < 0:
        raise ValueError("max_files must be non-negative")
    _ensure_readable_directory(root)

    allowed = ARCHIVE_SUFFIXES if suffixes is None else normalize_suffixes(suffixes)
    deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds
    found: list[Path] = []

    def check_deadline() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise ScanLimitError("archive scan deadline exceeded", reason="deadline")

    def record(path: Path) -> None:
        check_deadline()
        found.append(path)
        if max_files is not None and len(found) > max_files:
            raise ScanLimitError(
                f"archive scan exceeded max_files={max_files}",
                reason="max_files",
            )

    if not recursive:
        for path in sorted(root.iterdir()):
            check_deadline()
            if path.is_file() and not path.is_symlink() and _matches_suffix(path, allowed):
                record(path)
        return found

    def on_walk_error(error: OSError) -> None:
        logger.warning("cannot read directory %s: %s", error.filename, error)

    for current, dirnames, filenames in os.walk(root, followlinks=False, onerror=on_walk_error):
        check_deadline()
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
            record(path)
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


def _scan_zip(
    path: Path,
    scan: ArchiveScan,
    encoding: str,
    fallback_encoding: str,
    max_line_bytes: int,
) -> Iterator[LineRecord]:
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir() or _is_zip_symlink(info):
                    continue
                try:
                    with archive.open(info) as stream:
                        yield from _iter_binary_lines(
                            stream,
                            info.filename,
                            Path(info.filename).name,
                            encoding,
                            fallback_encoding,
                            max_line_bytes,
                        )
                except _ZIP_MEMBER_ERRORS as exc:
                    # A zip central directory allows random access, so one
                    # damaged member must not abandon the remaining members.
                    scan.member_errors.append(ArchiveMemberError(info.filename, str(exc)))
                else:
                    scan.succeeded_members.add(info.filename)
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ArchiveFormatError(f"invalid zip archive: {path}") from exc


def _is_tar_name(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in TAR_SUFFIXES)


def _scan_tar(
    path: Path,
    scan: ArchiveScan,
    encoding: str,
    fallback_encoding: str,
    max_line_bytes: int,
) -> Iterator[LineRecord]:
    try:
        with tarfile.open(path, mode="r|*") as archive:
            try:
                for member in archive:
                    if not member.isfile():
                        continue
                    try:
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
                    except _TAR_ERRORS as exc:
                        # Streaming tar cannot reliably seek past a damaged
                        # member, so stop here and let the caller keep the
                        # members that were already read.
                        scan.member_errors.append(
                            ArchiveMemberError(member.name, str(exc), scan_incomplete=True)
                        )
                        return
                    except OSError as exc:
                        if isinstance(
                            exc, (FileNotFoundError, PermissionError, IsADirectoryError)
                        ):
                            raise
                        scan.member_errors.append(
                            ArchiveMemberError(member.name, str(exc), scan_incomplete=True)
                        )
                        return
                    scan.succeeded_members.add(member.name)
            except _TAR_ERRORS as exc:
                scan.member_errors.append(
                    ArchiveMemberError(None, str(exc), scan_incomplete=True)
                )
                return
    except _TAR_ERRORS as exc:
        raise ArchiveFormatError(f"invalid tar archive: {path}") from exc
    except OSError as exc:
        if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError)):
            raise
        raise ArchiveFormatError(f"invalid tar archive: {path}") from exc


def _scan_single_compressed(
    path: Path,
    scan: ArchiveScan,
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
        scan.succeeded_members.add(path.stem)
    except (EOFError, lzma.LZMAError, gzip.BadGzipFile) as exc:
        raise ArchiveFormatError(f"invalid compressed file: {path}") from exc
    except OSError as exc:
        if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError)):
            raise
        raise ArchiveFormatError(f"invalid compressed file: {path}") from exc


def _scan_raw(
    path: Path,
    scan: ArchiveScan,
    encoding: str,
    fallback_encoding: str,
    max_line_bytes: int,
) -> Iterator[LineRecord]:
    with path.open("rb") as stream:
        yield from _iter_binary_lines(
            stream,
            path.name,
            path.name,
            encoding,
            fallback_encoding,
            max_line_bytes,
        )
    scan.succeeded_members.add(path.name)


def scan_archive(
    path: Path,
    encoding: str = "utf-8",
    fallback_encoding: str = "gb18030",
    max_line_bytes: int = 1_048_576,
) -> ArchiveScan:
    """Stream one archive while reporting which members could be read.

    Consume ``scan.lines`` to completion before inspecting the member
    outcomes. Container-level format errors still raise ``ArchiveFormatError``;
    damaged members are recorded instead so the worker can finish the healthy
    members and mark the task ``partial``.
    """
    path = Path(path)
    scan = ArchiveScan(lines=iter(()))
    suffix = path.suffix.lower()
    if suffix == ".zip":
        scan.lines = _scan_zip(path, scan, encoding, fallback_encoding, max_line_bytes)
    elif _is_tar_name(path.name):
        # The name declares a tar container; never fall back to decoding the
        # bytes as a single compressed log stream.
        scan.lines = _scan_tar(path, scan, encoding, fallback_encoding, max_line_bytes)
    elif suffix in SINGLE_COMPRESSED_SUFFIXES:
        scan.lines = _scan_single_compressed(
            path, scan, encoding, fallback_encoding, max_line_bytes
        )
    elif suffix in RAW_SUFFIXES:
        scan.lines = _scan_raw(path, scan, encoding, fallback_encoding, max_line_bytes)
    else:
        raise ValueError(f"unsupported archive type: {path}")
    return scan


def iter_archive_lines(
    path: Path,
    encoding: str = "utf-8",
    fallback_encoding: str = "gb18030",
    max_line_bytes: int = 1_048_576,
) -> Iterator[LineRecord]:
    """Strict line-only view: any damaged member fails the whole iteration."""
    scan = scan_archive(path, encoding, fallback_encoding, max_line_bytes)
    yield from scan.lines
    if scan.member_errors:
        first = scan.member_errors[0]
        location = first.member_path or "<archive>"
        raise ArchiveFormatError(f"archive member failed: {location}: {first.message}")
