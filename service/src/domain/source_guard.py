"""无副作用地判断 NAS 挂载是否真的只读。

规格 §7.2 要求"启动/健康检查时校验 NAS 挂载为只读；如果发现可写，拒绝处理并告警"。
这里刻意不使用写探针：在只读不变量下创建一个临时文件本身就是违规，删除失败还会在
用户的源数据上留下残渣。可用的证据只有两种，且都不写、不改属性：

1. POSIX 挂载表（``/proc/self/mounts``、``/etc/mtab``）里最长前缀命中挂载点的选项；
2. 文件系统反映到 ``st_mode`` 上的写权限位（弱证据，只能证明"读不出可写"）。

两者都拿不到结论时返回 ``unknown``，由上层如实展示。``unknown`` 永远不等于只读：
给用户一个假的绿勾比承认判定不了更糟。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import structlog

READ_ONLY: Final = "read_only"
WRITABLE: Final = "writable"
UNKNOWN: Final = "unknown"

POLICY_ENFORCE: Final = "enforce"
POLICY_WARN: Final = "warn"
POLICY_IGNORE: Final = "ignore"
READ_ONLY_POLICIES: Final = frozenset({POLICY_ENFORCE, POLICY_WARN, POLICY_IGNORE})

MOUNT_TABLE_CANDIDATES: Final = ("/proc/self/mounts", "/etc/mtab")
POSIX_PLATFORMS: Final = ("linux", "darwin")
WRITE_BITS: Final = 0o222
# /proc/mounts 用八进制转义空白与反斜杠，字段本身不能直接 split 完就当路径用。
_MOUNT_ESCAPES: Final = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
_MOUNT_ESCAPE_RE: Final = re.compile(r"\\(040|011|012|134)")

_logger = structlog.get_logger(__name__)


class SourceNotReadOnly(RuntimeError):
    """数据源挂载被判定为可写：按规格拒绝在其上规划或处理任务。"""


def read_mount_table() -> list[str] | None:
    """返回挂载表行；非 POSIX 或所有候选都读不到时返回 ``None``（不猜测）。"""
    if not sys.platform.startswith(POSIX_PLATFORMS):
        return None
    for candidate in MOUNT_TABLE_CANDIDATES:
        try:
            with Path(candidate).open(encoding="utf-8", errors="replace") as handle:
                return handle.read().splitlines()
        except OSError:
            continue
    return None


def _unescape_mount_path(raw: str) -> str:
    return _MOUNT_ESCAPE_RE.sub(lambda match: _MOUNT_ESCAPES[match.group(1)], raw)


def _match_key(value: str) -> str:
    """统一分隔符，让同一套前缀匹配逻辑在任何平台上都可被测试覆盖。"""
    return value.replace("\\", "/")


def select_mount_options(lines: Sequence[str], target: str) -> list[str] | None:
    """在挂载表行里做最长前缀匹配，返回命中挂载的选项列表；没命中返回 ``None``。"""
    wanted = _match_key(target)
    best_mount: str | None = None
    best_options: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        mount_point = _match_key(_unescape_mount_path(parts[1]))
        prefix = mount_point.rstrip("/") + "/"
        if wanted != mount_point and not wanted.startswith(prefix):
            continue
        if best_mount is None or len(mount_point) > len(best_mount):
            best_mount, best_options = mount_point, parts[3].split(",")
    if best_mount is None:
        return None
    return best_options


def mount_options_for(path: Path) -> list[str] | None:
    """``path`` 所在挂载点的选项列表；判不出来返回 ``None`` 而不是猜一个结论。"""
    lines = read_mount_table()
    if lines is None:
        return None
    try:
        target = str(path.resolve())
    except OSError:
        return None
    return select_mount_options(lines, target)


def writability_from_mode(mode: int) -> str:
    """只用 ``st_mode`` 的写位做弱判定：没有任何写位才算只读，否则只能 unknown。"""
    return UNKNOWN if mode & WRITE_BITS else READ_ONLY


def mode_writability(path: Path) -> str:
    try:
        mode = path.stat().st_mode
    except OSError:
        return UNKNOWN
    return writability_from_mode(mode)


def detect_writability(path: Path) -> str:
    """三态只读判定；返回值恒为 ``read_only`` / ``writable`` / ``unknown`` 之一。"""
    options = mount_options_for(path)
    if options is not None:
        if "ro" in options:
            return READ_ONLY
        if "rw" in options:
            # 挂载表显式 rw 才是 writable；其余选项组合一律降级为 unknown。
            return WRITABLE
        return UNKNOWN
    return mode_writability(path)


def guard_source(path: Path, *, policy: str = POLICY_ENFORCE, logger: Any | None = None) -> str:
    """按策略校验数据源目录并返回判定值。

    ``enforce`` 下发现可写就抛 :class:`SourceNotReadOnly`；``warn`` 只告警；``ignore``
    完全沉默（由运维显式选择）。任何策略都不会把 ``unknown`` 当成"已确认只读"，
    也不会为了探测而写数据源。
    """
    normalized = policy.strip().lower()
    if normalized not in READ_ONLY_POLICIES:
        raise ValueError(f"nas_read_only_policy must be one of {sorted(READ_ONLY_POLICIES)}")
    verdict = detect_writability(path)
    if verdict != READ_ONLY and normalized != POLICY_IGNORE:
        # 把"我不能确认只读"说出来，而不是给用户一个假的安心感。
        (logger or _logger).warning(
            "nas_read_only_not_confirmed", path=str(path), verdict=verdict, policy=normalized
        )
    if verdict == WRITABLE and normalized == POLICY_ENFORCE:
        raise SourceNotReadOnly(f"source path {path} is mounted writable; refusing to process")
    return verdict
