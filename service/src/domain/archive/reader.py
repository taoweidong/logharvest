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
from types import TracebackType
from typing import Protocol

from src.domain.archive.models import ArchiveFormatError, LineRecord, ScanLimitError

logger = logging.getLogger(__name__)

TAR_SUFFIXES = frozenset({".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz"})
SINGLE_COMPRESSED_SUFFIXES = frozenset({".gz", ".bz2", ".xz"})
RAW_SUFFIXES = frozenset({".log", ".txt"})
ARCHIVE_SUFFIXES = frozenset({".zip"}) | TAR_SUFFIXES | SINGLE_COMPRESSED_SUFFIXES

_TAR_ERRORS = (tarfile.TarError, EOFError, lzma.LZMAError, gzip.BadGzipFile)
_ZIP_MEMBER_ERRORS = (
    zipfile.BadZipFile,
    RuntimeError,
    NotImplementedError,
    EOFError,
    zlib.error,
)

DEFAULT_MAX_MEMBER_BYTES = 1 << 30
DEFAULT_MAX_TOTAL_BYTES = 4 << 30
DEFAULT_MAX_MEMBERS = 10_000
DEFAULT_MAX_RATIO = 500.0
# 压缩比只在解压量足够大时才有鉴别力：几 KB 的文本本来就能压出几百倍，
# 拿它去拦炸弹只会误伤正常的小文件，所以低于这个量不比压缩比。
RATIO_MIN_TOTAL_BYTES = 64 << 20


@dataclass(frozen=True)
class ArchiveBudget:
    """解压预算（规格 §11 的压缩炸弹限额）。

    四个维度全部是"解压后"的量：单成员字节、整包累计字节、成员个数、以及相对
    于源文件大小的膨胀比。超限一律终止扫描，绝不让一个十几 MB 的包把本地临时盘
    或 Worker 内存吃干净。

    单成员超限只牺牲该成员（其余成员照常产出，任务落 ``partial``）；整包级的
    字节/成员数/压缩比超限说明这个包本身就是炸弹或严重损坏，后续成员没有继续
    读的价值，直接停止扫描。
    """

    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_members: int = DEFAULT_MAX_MEMBERS
    max_ratio: float = DEFAULT_MAX_RATIO
    # 压缩比判定的起效门槛：低于这个解压量不比压缩比（详见模块常量处的说明）。
    ratio_min_total_bytes: int = RATIO_MIN_TOTAL_BYTES


DEFAULT_ARCHIVE_BUDGET = ArchiveBudget()


class _BudgetTracker:
    """在流式读取过程中累计解压量，超预算即抛 :class:`ScanLimitError`。

    这是唯一能在"读到一半"时发现炸弹的位置：提前读取 central directory 里声明的
    ``file_size`` 并不可信（zip 的声明值可以任意伪造），只有实际读出来的字节算数。
    """

    def __init__(self, budget: ArchiveBudget, source_size: int) -> None:
        self._budget = budget
        self._source_size = source_size
        self.total_bytes = 0
        self.member_count = 0
        self._member_bytes = 0

    def begin_member(self, member_path: str) -> None:
        self.member_count += 1
        self._member_bytes = 0
        if self.member_count > self._budget.max_members:
            raise ScanLimitError(
                f"archive exceeded max_members={self._budget.max_members} at {member_path!r}",
                reason="max_members",
            )

    def add_bytes(self, count: int, member_path: str) -> None:
        self._member_bytes += count
        self.total_bytes += count
        if self._member_bytes > self._budget.max_member_bytes:
            raise ScanLimitError(
                f"member {member_path!r} exceeded max_member_bytes={self._budget.max_member_bytes}",
                reason="member_bytes",
            )
        if self.total_bytes > self._budget.max_total_bytes:
            raise ScanLimitError(
                f"archive exceeded max_total_bytes={self._budget.max_total_bytes}",
                reason="total_bytes",
            )
        if (
            self.total_bytes >= self._budget.ratio_min_total_bytes
            and self._source_size > 0
            and self.total_bytes / self._source_size > self._budget.max_ratio
        ):
            raise ScanLimitError(
                f"archive expansion ratio exceeded max_ratio={self._budget.max_ratio}",
                reason="ratio",
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
    tracker: _BudgetTracker | None = None,
) -> Iterator[LineRecord]:
    line_no = 0
    while True:
        # Read room for a trailing CRLF so an exact-cap line is not truncated.
        raw = stream.readline(max_line_bytes + 2)
        if not raw:
            return
        if tracker is not None:
            tracker.add_bytes(len(raw), member_path)
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
    tracker: _BudgetTracker | None = None,
) -> Iterator[LineRecord]:
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir() or _is_zip_symlink(info):
                    continue
                try:
                    if tracker is not None:
                        tracker.begin_member(info.filename)
                    with archive.open(info) as stream:
                        yield from _iter_binary_lines(
                            stream,
                            info.filename,
                            Path(info.filename).name,
                            encoding,
                            fallback_encoding,
                            max_line_bytes,
                            tracker,
                        )
                except ScanLimitError as exc:
                    # 预算超限是这个包本身的问题：单成员超限只丢弃该成员，
                    # 整包级超限（总字节/成员数/压缩比）再读下去没有意义。
                    scan.member_errors.append(
                        ArchiveMemberError(
                            info.filename, str(exc), scan_incomplete=exc.reason != "member_bytes"
                        )
                    )
                    if exc.reason != "member_bytes":
                        return
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
    tracker: _BudgetTracker | None = None,
) -> Iterator[LineRecord]:
    try:
        with tarfile.open(path, mode="r|*") as archive:
            try:
                for member in archive:
                    if not member.isfile():
                        continue
                    try:
                        if tracker is not None:
                            tracker.begin_member(member.name)
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
                                tracker,
                            )
                    except ScanLimitError as exc:
                        # 流式 tar 无法可靠跳过一个成员的剩余字节，超预算即停止。
                        scan.member_errors.append(
                            ArchiveMemberError(member.name, str(exc), scan_incomplete=True)
                        )
                        return
                    except _TAR_ERRORS as exc:
                        # Streaming tar cannot reliably seek past a damaged
                        # member, so stop here and let the caller keep the
                        # members that were already read.
                        scan.member_errors.append(
                            ArchiveMemberError(member.name, str(exc), scan_incomplete=True)
                        )
                        return
                    except OSError as exc:
                        if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError)):
                            raise
                        scan.member_errors.append(
                            ArchiveMemberError(member.name, str(exc), scan_incomplete=True)
                        )
                        return
                    scan.succeeded_members.add(member.name)
                # tarfile silently treats an invalid later header as EOF. A
                # complete archive must still be followed by zero end blocks;
                # anything else means the scan stopped before the real end.
                archive_stream = archive.fileobj
                remainder = b"" if archive_stream is None else archive_stream.read(1024)
                if len(remainder) < 512 or any(remainder[:512]):
                    scan.member_errors.append(
                        ArchiveMemberError(
                            None,
                            "tar archive ended before its end-of-archive marker",
                            scan_incomplete=True,
                        )
                    )
            except _TAR_ERRORS as exc:
                scan.member_errors.append(ArchiveMemberError(None, str(exc), scan_incomplete=True))
                return
    except _TAR_ERRORS as exc:
        raise ArchiveFormatError(f"invalid tar archive: {path}") from exc
    except OSError as exc:
        if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError)):
            raise
        raise ArchiveFormatError(f"invalid tar archive: {path}") from exc


class _ReadableStream(Protocol):
    def readline(self, size: int = -1) -> bytes: ...
    def __enter__(self) -> "_ReadableStream": ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
        /,
    ) -> None: ...


def _open_single(path: Path) -> _ReadableStream:
    suffix = path.suffix.lower()
    if suffix == ".gz":
        return gzip.open(path, "rb")
    if suffix == ".bz2":
        return bz2.open(path, "rb")
    return lzma.open(path, "rb")


def _scan_single_compressed(
    path: Path,
    scan: ArchiveScan,
    encoding: str,
    fallback_encoding: str,
    max_line_bytes: int,
    tracker: _BudgetTracker | None = None,
) -> Iterator[LineRecord]:
    try:
        if tracker is not None:
            tracker.begin_member(path.stem)
        with _open_single(path) as stream:
            yield from _iter_binary_lines(
                stream,
                path.stem,
                path.stem,
                encoding,
                fallback_encoding,
                max_line_bytes,
                tracker,
            )
        scan.succeeded_members.add(path.stem)
    except ScanLimitError as exc:
        scan.member_errors.append(ArchiveMemberError(path.stem, str(exc), scan_incomplete=True))
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
    tracker: _BudgetTracker | None = None,
) -> Iterator[LineRecord]:
    if tracker is not None:
        tracker.begin_member(path.name)
    with path.open("rb") as stream:
        try:
            yield from _iter_binary_lines(
                stream,
                path.name,
                path.name,
                encoding,
                fallback_encoding,
                max_line_bytes,
                tracker,
            )
        except ScanLimitError as exc:
            scan.member_errors.append(ArchiveMemberError(path.name, str(exc), scan_incomplete=True))
            return
    scan.succeeded_members.add(path.name)


def _source_size_of(path: Path) -> int:
    """压缩比的分母：源文件在磁盘上的字节数；取不到就返回 0（跳过压缩比判定）。"""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def scan_archive(
    path: Path,
    encoding: str = "utf-8",
    fallback_encoding: str = "gb18030",
    max_line_bytes: int = 1_048_576,
    budget: ArchiveBudget | None = None,
) -> ArchiveScan:
    """Stream one archive while reporting which members could be read.

    Consume ``scan.lines`` to completion before inspecting the member
    outcomes. Container-level format errors still raise ``ArchiveFormatError``;
    damaged members are recorded instead so the worker can finish the healthy
    members and mark the task ``partial``.

    ``budget`` 是解压预算（规格 §11 的压缩炸弹限额）；``None`` 表示沿用
    :data:`DEFAULT_ARCHIVE_BUDGET`，传一个显式预算可以关掉某几个维度（例如把
    ``max_ratio`` 设为 ``float("inf")``）。
    """
    path = Path(path)
    scan = ArchiveScan(lines=iter(()))
    effective_budget = DEFAULT_ARCHIVE_BUDGET if budget is None else budget
    tracker = _BudgetTracker(effective_budget, _source_size_of(path))
    suffix = path.suffix.lower()
    if suffix == ".zip":
        scan.lines = _scan_zip(path, scan, encoding, fallback_encoding, max_line_bytes, tracker)
    elif _is_tar_name(path.name):
        # The name declares a tar container; never fall back to decoding the
        # bytes as a single compressed log stream.
        scan.lines = _scan_tar(path, scan, encoding, fallback_encoding, max_line_bytes, tracker)
    elif suffix in SINGLE_COMPRESSED_SUFFIXES:
        scan.lines = _scan_single_compressed(
            path, scan, encoding, fallback_encoding, max_line_bytes, tracker
        )
    elif suffix in RAW_SUFFIXES:
        scan.lines = _scan_raw(path, scan, encoding, fallback_encoding, max_line_bytes, tracker)
    else:
        raise ValueError(f"unsupported archive type: {path}")
    return scan


def iter_archive_lines(
    path: Path,
    encoding: str = "utf-8",
    fallback_encoding: str = "gb18030",
    max_line_bytes: int = 1_048_576,
    budget: ArchiveBudget | None = None,
) -> Iterator[LineRecord]:
    """Strict line-only view: any damaged member fails the whole iteration."""
    scan = scan_archive(path, encoding, fallback_encoding, max_line_bytes, budget)
    yield from scan.lines
    if scan.member_errors:
        first = scan.member_errors[0]
        location = first.member_path or "<archive>"
        raise ArchiveFormatError(f"archive member failed: {location}: {first.message}")
