"""LogHarvest 真实环境 E2E 的 NAS 夹具生成器（幂等 / 可复现 / 自带期望值）.

只依赖标准库。产出一棵固定的 ``e2e/nas/`` 目录树，并把每个用例的**期望值**
（文件数、行数、成员数、各规则命中数、成员级行数）写进
``e2e/artifacts/fixtures.json`` 作为数值 oracle。

期望值不是手抄的：本文件内置一份与后端规则模型等价的最小求值器
（``_line_matches``），对生成的每一行算一遍，因此夹具与 oracle 天然一致；
后端真实结果与之不符即为缺陷，而不是"期望写错了"。

用法::

    python e2e/make_nas.py                 # 全量（含 60MB 取消用例与 mock 日志）
    python e2e/make_nas.py --skip-mock     # 跳过 data/generate_mock_logs.py
    python e2e/make_nas.py --only clean     # 只重建某个用例（仍会重写 oracle）
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

E2E_DIR = Path(__file__).resolve().parent
REPO_DIR = E2E_DIR.parent
NAS_DIR = E2E_DIR / "nas"
ARTIFACTS_DIR = E2E_DIR / "artifacts"
MOCK_DIR = REPO_DIR / "data" / "logs"
MOCK_GENERATOR = REPO_DIR / "data" / "generate_mock_logs.py"

# 取消用例用的大压缩包目标未压缩字节数（zip 内 ZIP_STORED，落盘同尺寸）。
CANCEL_TARGET_BYTES = 60 * 1024 * 1024
# 紧预算 Worker（phase D）用：单成员 3 MiB，超过 1 MiB 预算即被牺牲。
LIMITS_HUGE_MEMBER_BYTES = 3 * 1024 * 1024

PERF_KEYWORD_RULE_COUNT = 5000
PERF_LINES = 5800
PERF_DENSE_TOKENS = 1000

# 长行截断用例的期望口径：等价于
# service/src/application/worker/processor.py 的 RAW_LINE_LIMIT 与
# _truncate_raw_line（processor.py:29,81-86；契约见 e2e/refs/api-contract.md §7 末）：
# 按 UTF-8 **字节数**判定，>4096 才截，截到前 4096 字节并用 errors="ignore"
# 丢弃切口处不完整的多字节字符；截断发生在规则匹配**之后**，匹配仍看整行。
RAW_LINE_LIMIT_BYTES = 4096


def _expected_raw_line(text: str) -> tuple[str, bool]:
    """processor._truncate_raw_line 的等价实现：返回(入库 raw_line, raw_truncated)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= RAW_LINE_LIMIT_BYTES:
        return text, False
    return encoded[:RAW_LINE_LIMIT_BYTES].decode("utf-8", errors="ignore"), True


def _noise(i: int) -> str:
    """生成一行不含任何受控关键字的噪声行（刻意避开 error/超时/rows=/alert=）."""
    pool = [
        "INFO  service heartbeat ok",
        "DEBUG cache warmed entries=10",
        "INFO request handled elapsed=120ms",
        "TRACE span=7f2a duration_us=812",
        "INFO queue depth=3 workers=2",
        "DEBUG gc pause=4ms heap=181MB",
    ]
    return f"{pool[i % len(pool)]} seq={i}"


# --------------------------------------------------------------------------
# canonical 规则集：与 service/src/domain/rules/models.py 的形状一致，
# harness 会把它原样 POST 给 /api/rule-sets。
# --------------------------------------------------------------------------
CANONICAL_RULES: list[dict[str, Any]] = [
    {
        "id": "error_kw",
        "name": "错误关键字",
        "enabled": True,
        "severity": "error",
        "tags": ["e2e", "kw"],
        "match": {"type": "keyword", "any": ["ERROR"]},
    },
    {
        "id": "slow_regex",
        "name": "慢请求阈值",
        "enabled": True,
        "severity": "warning",
        "tags": ["e2e", "threshold"],
        "match": {
            "type": "regex",
            "pattern": r"elapsed=(?P<elapsed_ms>\d+)ms",
            "flags": "",
            "timeout_ms": 50,
        },
        "threshold": {"field": "elapsed_ms", "op": "gt", "value": "5000"},
    },
    {
        "id": "cn_timeout",
        "name": "中文超时（gb18030 回退）",
        "enabled": True,
        "severity": "info",
        "tags": ["e2e", "encoding"],
        "match": {"type": "keyword", "any": ["超时"]},
    },
    {
        "id": "conn_refused",
        "name": "连接被拒（多关键字 all）",
        "enabled": True,
        "severity": "error",
        "tags": ["e2e", "kw"],
        "match": {"type": "keyword", "all": ["connection", "refused"]},
    },
    {
        "id": "big_rows",
        "name": "大结果集",
        "enabled": True,
        "severity": "info",
        "tags": ["e2e", "threshold"],
        "match": {"type": "regex", "pattern": r"rows=(?P<rows>\d+)", "flags": ""},
        "threshold": {"field": "rows", "op": "gte", "value": "1000"},
    },
    {
        "id": "alert_ci",
        "name": "告警级别（忽略大小写）",
        "enabled": True,
        "severity": "warning",
        "tags": ["e2e", "flags"],
        "match": {"type": "regex", "pattern": r"ALERT=(?P<alert_level>\w+)", "flags": "i"},
    },
    {
        "id": "disabled_rule",
        "name": "禁用规则（TOMBSTONE）",
        "enabled": False,
        "severity": "error",
        "tags": ["e2e"],
        "match": {"type": "keyword", "any": ["TOMBSTONE"]},
    },
]

CANONICAL_RULE_SET: dict[str, Any] = {
    "name": "e2e-canonical",
    "file_glob": "**/*",
    "encoding": "utf-8",
    "fallback_encoding": "gb18030",
    "rules": CANONICAL_RULES,
}

# 5000 规则性能用例的规则集（关键词规则 kwNNNN;，定宽 + 分号，互不为子串）。
PERF_KEYWORDS = [f"kw{i:06d};" for i in range(PERF_KEYWORD_RULE_COUNT)]
PERF_RULES: list[dict[str, Any]] = [
    {
        "id": f"kw_{i:06d}",
        "name": f"信号 {i}",
        "enabled": True,
        "severity": "info",
        "tags": ["perf"],
        "match": {"type": "keyword", "any": [keyword]},
    }
    for i, keyword in enumerate(PERF_KEYWORDS)
] + CANONICAL_RULES[:1]
PERF_RULE_SET: dict[str, Any] = {
    "name": "e2e-perf-5k",
    "file_glob": "**/*",
    "encoding": "utf-8",
    "fallback_encoding": "gb18030",
    "rules": PERF_RULES,
}


# --------------------------------------------------------------------------
# 内置求值器：与后端 RuleSet 语义等价的最小实现（keyword any/all +
# case_sensitive=False，regex 命名组 + threshold 比较，flags 解析）。
# --------------------------------------------------------------------------
_FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE, "u": re.UNICODE}
_OPS = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}
_COMPILED: dict[tuple[str, str], re.Pattern[str]] = {}


def _compile(pattern: str, flags: str) -> re.Pattern[str]:
    key = (pattern, flags)
    cached = _COMPILED.get(key)
    if cached is None:
        value = 0
        for char in (flags or "").lower():
            value |= _FLAG_MAP.get(char, 0)
        cached = _COMPILED[key] = re.compile(pattern, value)
    return cached


def _rule_matches(rule: dict[str, Any], text: str) -> bool:
    match = rule["match"]
    if match["type"] == "keyword":
        lowered = text.lower()
        if "all" in match:
            return all(keyword.lower() in lowered for keyword in match["all"])
        return any(keyword.lower() in lowered for keyword in match.get("any", []))
    if match["type"] != "regex":
        raise ValueError(f"unknown match type {match['type']!r}")
    found = _compile(match["pattern"], match.get("flags", "")).search(text)
    if found is None:
        return False
    threshold = rule.get("threshold")
    if threshold is None:
        return True
    group = found.groupdict().get(threshold["field"])
    if group is None:
        return False
    try:
        left = Decimal(group)
        right = Decimal(str(threshold["value"]))
    except Exception:  # noqa: BLE001 - 非数值字段等价于不命中
        return False
    return _OPS[threshold["op"]](left, right)


def _line_matches(line: str, rules: list[dict[str, Any]]) -> list[str]:
    return [rule["id"] for rule in rules if rule.get("enabled", True) and _rule_matches(rule, line)]


# --------------------------------------------------------------------------
# 归档写入工具
# --------------------------------------------------------------------------
def _reset_dir(path: Path) -> None:
    """幂等：用例目录先删后建，避免上一轮残留文件混进扫描结果."""
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _join(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


def _text_of(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _zip_bytes(
    members: list[tuple[str, bytes]],
    *,
    stored: bool = False,
    symlinks: dict[str, str] | None = None,
    odd_names: list[tuple[str, bytes]] | None = None,
) -> bytes:
    """按写入顺序产出 zip；``symlinks``/``odd_names`` 用于构造越界与符号链接成员."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
        for name, data in odd_names or []:
            zf.writestr(name, data)
        for link, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(link)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target.encode("utf-8"))
    return buffer.getvalue()


def _write_zip(
    path: Path,
    members: list[tuple[str, bytes]],
    *,
    stored: bool = False,
    symlinks: dict[str, str] | None = None,
    odd_names: list[tuple[str, bytes]] | None = None,
    corrupt: list[str] | None = None,
) -> None:
    data = bytearray(
        _zip_bytes(members, stored=stored, symlinks=symlinks, odd_names=odd_names)
    )
    for name in corrupt or []:
        _corrupt_zip_entry(data, name)
    path.write_bytes(bytes(data))
    if corrupt:
        _assert_zip_corruption(path, set(corrupt))


def _assert_zip_corruption(path: Path, corrupt: set[str]) -> None:
    """夹具自检：损坏成员必须读失败，其余成员必须完好，中央目录必须可解析."""
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if not corrupt <= names:
            raise AssertionError(f"{path.name} 缺少待损坏成员 {corrupt} / {names}")
        for name in corrupt:
            try:
                zf.read(name)
                raise AssertionError(f"{path.name}!{name} 损坏夹具竟然可读，无法测成员牺牲")
            except AssertionError:
                raise
            except Exception:  # noqa: BLE001 - 任何读取异常都符合预期
                pass
        for name in names - corrupt:
            if zf.getinfo(name).is_dir():
                continue
            try:
                zf.read(name)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"{path.name}!{name} 被连带损坏：{exc}") from exc


def _corrupt_zip_entry(data: bytearray, member_name: str) -> None:
    """破坏某个 deflated 成员的压缩流（原地异或），使其读取时必然报错.

    用本地文件头里的 name_len/extra_len 精确定位数据起点，跳过前 8 字节后
    在 ``compress_size`` 范围内异或 —— 越界会连带损坏中央目录，那样整个
    归档都读不了，测的就不是"牺牲单个成员"而是"归档不可读"了。
    """
    target = member_name.encode("utf-8")
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
        info = next(item for item in zf.infolist() if item.filename == member_name)
        if info.compress_type == zipfile.ZIP_STORED:
            raise ValueError(f"{member_name} 必须是 deflated 才能构造损坏成员")
    head = info.header_offset
    if data[head : head + 4] != b"PK\x03\x04":
        raise AssertionError(f"{member_name} 本地头魔数异常")
    name_len = int.from_bytes(data[head + 26 : head + 28], "little")
    extra_len = int.from_bytes(data[head + 28 : head + 30], "little")
    if data[head + 30 : head + 30 + name_len] != target:
        raise AssertionError(f"{member_name} 本地头名称不匹配")
    data_start = head + 30 + name_len + extra_len
    start = data_start + 8
    end = min(start + 64, data_start + info.compress_size - 8)
    if end <= start:
        raise AssertionError(f"{member_name} 压缩段过短，无法在安全范围内构造损坏")
    for index in range(start, end):
        data[index] ^= 0x5A


def _write_tar(
    path: Path,
    members: list[tuple[str, bytes]],
    *,
    mode: str = "w:gz",
    symlinks: dict[str, str] | None = None,
    odd_names: list[tuple[str, bytes]] | None = None,
) -> None:
    with tarfile.open(path, mode) as tf:
        for name, data in members + list(odd_names or []):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 1_730_000_000
            tf.addfile(info, io.BytesIO(data))
        for link, target in (symlinks or {}).items():
            info = tarfile.TarInfo(link)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            info.mtime = 1_730_000_000
            tf.addfile(info)


def _write_gz(path: Path, lines: list[str], *, truncate_ratio: float | None = None) -> None:
    payload = gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))
    if truncate_ratio is None:
        path.write_bytes(payload)
    else:
        path.write_bytes(payload[: int(len(payload) * truncate_ratio)])


# --------------------------------------------------------------------------
# oracle：Case 把"应当被读到的成员"逐条登记，命中数由内置求值器算出
# --------------------------------------------------------------------------
class Case:
    """一个用例目录的产物 + 由内置求值器算出的期望值."""

    def __init__(
        self,
        name: str,
        *,
        source: str | None = None,
        rules: list[dict[str, Any]] | None = None,
        expect_total_files: int | None = None,
        note: str = "",
    ) -> None:
        self.name = name
        self.source = source or f"/nas/{name}"
        self.rules = CANONICAL_RULES if rules is None else rules
        self.note = note
        self.dir = NAS_DIR / name
        self._members: dict[str, dict[str, dict[str, Any]]] = {}
        self._skipped: dict[str, list[dict[str, str]]] = {}
        self.expect_total_files = expect_total_files
        self.file_status: dict[str, str] = {}
        self.loose: dict[str, Any] = {}

    def member(self, archive: str, path: str, lines: list[str]) -> int:
        """登记一个应当被读取的成员，并按规则集逐行算命中."""
        hits: dict[str, int] = {}
        for line in lines:
            for rule_id in _line_matches(line, self.rules):
                hits[rule_id] = hits.get(rule_id, 0) + 1
        entry = self._members.setdefault(archive, {})
        entry[path] = {"lines": len(lines), "hits": hits, "hits_total": sum(hits.values())}
        self.file_status.setdefault(archive, "succeeded")
        return len(lines)

    def skipped(self, archive: str, path: str, reason: str) -> None:
        """登记预期被跳过的成员（越界、符号链接、不支持格式）."""
        self._skipped.setdefault(archive, []).append({"path": path, "reason": reason})

    def status(self, archive: str, value: str) -> None:
        self.file_status[archive] = value

    def to_dict(self) -> dict[str, Any]:
        per_file: dict[str, Any] = {}
        for archive, members in self._members.items():
            # 终态为 unknown（截断/损坏的整包）时，"读到了多少行"取决于压缩流的可解
            # 前缀——那是 gzip deflate 块边界决定的，不是夹具能保证的数，所以记成 None
            # 表示"不参与断言"。能保证的是另一件事：**没读完就不得发布命中**，
            # 于是命中数断言为 0，而不是按全量成员内容去猜一个正数。
            unpublished = self.file_status.get(archive) == "unknown"
            per_file[archive] = {
                "expected_status": self.file_status.get(archive, "succeeded"),
                "lines": None if unpublished else sum(item["lines"] for item in members.values()),
                "matches": 0 if unpublished else sum(item["hits_total"] for item in members.values()),
                "members": {
                    path: {
                        "lines": None if unpublished else item["lines"],
                        "matches": 0 if unpublished else item["hits_total"],
                        "hits": {} if unpublished else item["hits"],
                    }
                    for path, item in sorted(members.items())
                },
            }
        totals: dict[str, int] = {}
        for info in per_file.values():
            for member in info["members"].values():
                for rule_id, count in member["hits"].items():
                    totals[rule_id] = totals.get(rule_id, 0) + count
        result: dict[str, Any] = {
            "source": self.source,
            "archives": sorted(per_file),
            "expect_total_files": (
                len(per_file) if self.expect_total_files is None else self.expect_total_files
            ),
            # 任一归档行数不可预测，整个用例的总行数就不预测（None 由 run_case 跳过）。
            "expect_total_lines": (
                None
                if any(info["lines"] is None for info in per_file.values())
                else sum(info["lines"] for info in per_file.values())
            ),
            "expect_total_matches": sum(info["matches"] for info in per_file.values()),
            "expect_matches_by_rule": dict(sorted(totals.items())),
            "per_file": per_file,
            "file_statuses": dict(sorted(self.file_status.items())),
            "skipped_members": {k: v for k, v in sorted(self._skipped.items())},
        }
        if self.note:
            result["note"] = self.note
        result.update(self.loose)
        return result


def _rule_lines(*, errors: int = 0, slow: int = 0, plain: int = 3) -> list[str]:
    """构造一段行：plain 行噪声 + errors 个 ERROR 行 + slow 个超阈值慢行."""
    lines = [_noise(i) for i in range(plain)]
    lines += [f"ERROR component={i} code={5000 + i}" for i in range(errors)]
    lines += [f"INFO slow op={i} elapsed={6000 + i * 10}ms" for i in range(slow)]
    return lines


def build_clean() -> dict[str, Any]:
    case = Case("clean")
    _reset_dir(case.dir)

    app = [
        "INFO boot sequence start",
        "ERROR db unavailable",
        "INFO heartbeat",
        "INFO query elapsed=6000ms plan=seq",
        "DEBUG cache warmed entries=10",
        "ERROR auth failed for user=42",
    ]
    app2 = [
        "INFO plain start",
        "ERROR slow commit elapsed=8000ms",
        "WARN disk usage 71%",
        "alert=warning from watchdog",
        "INFO done",
    ]
    deep = [
        "ERROR timeout connecting to peer",
        "INFO heartbeat",
        "INFO batch elapsed=5001ms",
        "connection refused by gateway",
        "rows=1500 bulk insert",
    ]
    svc = [
        "INFO svc start",
        "ERROR oom killer invoked",
        "ERROR disk full on /data",
        "INFO heartbeat",
        "ALERT=CRITICAL from monitor",
        "INFO report elapsed=9000ms",
        "INFO svc stop",
    ]
    legacy = ["INFO legacy", "rows=999 small select", "INFO legacy end"]

    _write_zip(
        case.dir / "app.zip",
        [("app.log", _join(app)), ("app2.log", _join(app2)), ("nested/deep.log", _join(deep))],
    )
    case.member("app.zip", "app.log", app)
    case.member("app.zip", "app2.log", app2)
    case.member("app.zip", "nested/deep.log", deep)

    _write_tar(case.dir / "svc.tar.gz", [("svc/svc.log", _join(svc)), ("svc/legacy.log", _join(legacy))])
    case.member("svc.tar.gz", "svc/svc.log", svc)
    case.member("svc.tar.gz", "svc/legacy.log", legacy)
    case.loose["nested_member"] = "nested/deep.log"
    case.loose["glob_case"] = {
        "file_glob": "nested/*",
        "expect_members_prefix": "nested/",
        "note": "成员级 file_glob 收窄后，命中必须全部落在 nested/ 下",
    }
    return case.to_dict()


def build_formats() -> dict[str, Any]:
    case = Case("formats", note="多格式提取：tgz/bz2/xz/tar/gz/zip 逐一验证")
    _reset_dir(case.dir)
    spec = [
        ("a.tgz", "a.log", "tar:gz"),
        ("b.tar.bz2", "b.log", "tar:bz2"),
        ("c.tar.xz", "c.log", "tar:xz"),
        ("d.tar", "d.log", "tar"),
        ("f.zip", "f.log", "zip"),
    ]
    for archive, member, kind in spec:
        lines = [f"ERROR format={kind}", _noise(1), _noise(2)]
        path = case.dir / archive
        payload = _join(lines)
        if kind == "zip":
            _write_zip(path, [(member, payload)])
        else:
            mode = {"tar:gz": "w:gz", "tar:bz2": "w:bz2", "tar:xz": "w:xz", "tar": "w"}[kind]
            _write_tar(path, [(member, payload)], mode=mode)
        case.member(archive, member, lines)
    gz_lines = ["ERROR format=single-gz", _noise(3)]
    _write_gz(case.dir / "e.log.gz", gz_lines)
    case.member("e.log.gz", "<single>", gz_lines)
    case.loose["formats_supported"] = sorted([archive for archive, _m, _k in spec] + ["e.log.gz"])
    case.loose["single_gz_note"] = "单文件 gzip 的成员名由后端决定，运行时记录在 observed.json"
    return case.to_dict()


def build_partial() -> dict[str, Any]:
    case = Case("partial", note="zip 内单个成员损坏：牺牲该成员、保留其余、任务 partial")
    _reset_dir(case.dir)
    good = [
        "ERROR bad disk 1",
        "ERROR bad disk 2",
        "INFO heartbeat",
        "ERROR bad disk 3",
        "INFO slow elapsed=7000ms",
        "ERROR bad disk 4",
    ]
    bad = [f"ERROR corrupt-member-{i}" for i in range(4)]
    good_tar = ["INFO tar ok", "ERROR tar member", "INFO heartbeat"]
    _write_zip(
        case.dir / "partial.zip",
        [("good.log", _join(good)), ("bad.log", _join(bad))],
        corrupt=["bad.log"],
    )
    case.member("partial.zip", "good.log", good)
    case.skipped("partial.zip", "bad.log", "deflate 流被异或破坏，读取必报错")
    case.status("partial.zip", "partial")
    _write_tar(case.dir / "good.tar.gz", [("ok/ok.log", _join(good_tar))])
    case.member("good.tar.gz", "ok/ok.log", good_tar)
    case.loose["expect_partial_member"] = "bad.log"
    return case.to_dict()


def build_truncated() -> dict[str, Any]:
    case = Case("truncated", note="gzip 流被截断：唯一成员读不完 -> 任务非 succeeded")
    _reset_dir(case.dir)
    lines = _rule_lines(errors=8, plain=40)
    _write_gz(case.dir / "cut.gz", lines, truncate_ratio=0.6)
    case.member("cut.gz", "<single>", lines)
    case.status("cut.gz", "unknown")
    case.loose["assert"] = "status != succeeded 且 error 非空"
    return case.to_dict()


def build_empty() -> dict[str, Any]:
    case = Case(
        "empty",
        expect_total_files=0,
        note="0 归档目录：隐藏目录/不支持后缀不参与扫描，作业直接 succeeded",
    )
    _reset_dir(case.dir)
    (case.dir / ".hidden").mkdir()
    _write_zip(case.dir / ".hidden" / "hidden.zip", [("h.log", _join(_rule_lines(errors=1)))])
    (case.dir / "notes.txt").write_text("not an archive\n", encoding="utf-8")
    (case.dir / "nested" / "empty").mkdir(parents=True)
    case.loose["ignored"] = [".hidden/hidden.zip", "notes.txt"]
    return case.to_dict()


def build_misc() -> dict[str, Any]:
    case = Case(
        "misc",
        note="不支持的后缀（.7z/.md/.json）被忽略，同目录真实 zip 正常处理",
        expect_total_files=1,
    )
    _reset_dir(case.dir)
    (case.dir / "readme.md").write_text("# doc\n", encoding="utf-8")
    (case.dir / "data.json").write_text('{"a": 1}\n', encoding="utf-8")
    (case.dir / "archive.7z").write_bytes(b"7z\xbc\xaf\x27\x1c\x00\x00junk")
    lines = _rule_lines(errors=1, plain=2)
    _write_zip(case.dir / "real.zip", [("m.log", _join(lines))])
    case.member("real.zip", "m.log", lines)
    case.loose["misc_ignored"] = ["readme.md", "data.json", "archive.7z"]
    return case.to_dict()


def build_deep() -> dict[str, Any]:
    case = Case("deep", note="max_depth=5：l1..l5 的归档被记录，l6/l7 被剪枝")
    _reset_dir(case.dir)
    for level in range(1, 6):
        current = case.dir
        for step in range(1, level + 1):
            current = current / f"l{step}"
        current.mkdir(parents=True, exist_ok=True)
        lines = _rule_lines(errors=1, plain=1)
        _write_zip(current / f"d{level}.zip", [("x.log", _join(lines))])
        case.member(f"d{level}.zip", "x.log", lines)
    deepest = case.dir
    for step in range(1, 8):
        deepest = deepest / f"l{step}"
    deepest.mkdir(parents=True, exist_ok=True)
    d7 = _rule_lines(errors=5, plain=1)
    _write_zip(deepest / "d7.zip", [("x.log", _join(d7))])
    case.loose["pruned"] = ["l1/l2/l3/l4/l5/l6/l7/d7.zip"]
    # d7 不进默认期望（max_depth 默认 5 时被剪枝），单独给一份"放宽到 20 层"的期望，
    # 这样 e2e 能用同一套 Oracle 证明"少扫 1 个文件是深度裁剪而不是漏扫 bug"。
    d7_hits: dict[str, int] = {}
    for line in d7:
        for rule_id in _line_matches(line, case.rules):
            d7_hits[rule_id] = d7_hits.get(rule_id, 0) + 1
    base = case.to_dict()
    full_by_rule = dict(base["expect_matches_by_rule"])
    for rule_id, count in d7_hits.items():
        full_by_rule[rule_id] = full_by_rule.get(rule_id, 0) + count
    case.loose["deep_full"] = {
        "file_filter": {"recursive": True, "max_depth": 20},
        "expect_total_files": base["expect_total_files"] + 1,
        "expect_total_lines": base["expect_total_lines"] + len(d7),
        "expect_total_matches": base["expect_total_matches"] + sum(d7_hits.values()),
        "expect_matches_by_rule": full_by_rule,
        "extra_archive": "d7.zip",
        "extra_lines": len(d7),
        "extra_matches": sum(d7_hits.values()),
    }
    return case.to_dict()


def build_encoding() -> dict[str, Any]:
    case = Case("encoding", note="utf-8 主编码 + gb18030 回退：中文日志必须可匹配")
    _reset_dir(case.dir)
    cn_lines = [f"ERROR 中文服务 超时 第{i}次重试" for i in range(1, 6)]
    en_lines = ["INFO utf-8 member", "ERROR plain english", "INFO heartbeat"]
    cn_bytes = ("\n".join(cn_lines) + "\n").encode("gb18030")
    try:
        cn_bytes.decode("utf-8")
        raise AssertionError("gb18030 夹具必须不是合法 utf-8，否则测不出回退编码")
    except UnicodeDecodeError:
        pass
    _write_zip(case.dir / "cn.zip", [("cn.log", cn_bytes), ("en.log", _join(en_lines))])
    case.member("cn.zip", "cn.log", cn_lines)
    case.member("cn.zip", "en.log", en_lines)
    return case.to_dict()


def build_traversal() -> dict[str, Any]:
    case = Case(
        "traversal",
        note=(
            "越界成员名（../、sub/../../）在流式读取下只是字符串：Worker 从不解包落盘，"
            "所以它们的内容照样算作数据；符号链接成员必须被跳过，越界名不得产生任何落盘文件"
        ),
    )
    _reset_dir(case.dir)
    ok = _rule_lines(errors=1, plain=1)
    ok2 = _rule_lines(errors=1, slow=1, plain=1)
    escape = [f"ERROR SHOULD-NOT-BE-READ {i}" for i in range(5)]
    _write_zip(
        case.dir / "evil.zip",
        [("ok.log", _join(ok))],
        odd_names=[("../escape.log", _join(escape)), ("sub/../../escape2.log", _join(escape))],
        symlinks={"link.log": "../escape.log"},
    )
    # `archive.open(info)` 走内存流，成员名里的 `..` 不参与任何路径拼接，因此越界名
    # 是被读取的普通成员。反过来，符号链接成员（menu_type lnk）必须整条跳过：如果
    # 跟链读，escape 的 5 行会被再数一遍，总行数从 12 变 17——行数本身就是证据。
    case.member("evil.zip", "ok.log", ok)
    case.member("evil.zip", "../escape.log", escape)
    case.member("evil.zip", "sub/../../escape2.log", escape)
    case.skipped("evil.zip", "link.log", "符号链接成员")
    _write_tar(
        case.dir / "evil.tar",
        [("ok2.log", _join(ok2))],
        odd_names=[("../t.escape.log", _join(escape))],
        symlinks={"tlink.log": "../t.escape.log"},
    )
    case.member("evil.tar", "ok2.log", ok2)
    case.member("evil.tar", "../t.escape.log", escape)
    case.skipped("evil.tar", "tlink.log", "符号链接成员")
    case.loose["must_not_exist"] = ["/nas/escape.log", "/nas/escape2.log", "/nas/t.escape.log"]
    return case.to_dict()


def build_modified() -> dict[str, Any]:
    case = Case("modified", note="作业创建后追加字节：执行期校验不符 -> permanent failed")
    _reset_dir(case.dir)
    lines = _rule_lines(errors=2, plain=2)
    _write_zip(case.dir / "mod.zip", [("mod.log", _join(lines))])
    case.member("mod.zip", "mod.log", lines)
    return case.to_dict()


def build_flood() -> dict[str, Any]:
    case = Case("flood", note="大命中量：默认上限全量入库；紧预算 Worker 触发上限即失败清场")
    _reset_dir(case.dir)
    lines = [_noise(i) for i in range(500)]
    lines += [f"ERROR flood {i}" for i in range(6500)]
    _write_zip(case.dir / "flood.zip", [("flood.log", _join(lines))])
    case.member("flood.zip", "flood.log", lines)
    case.loose["flood_error_lines"] = sum(1 for line in lines if "ERROR" in line)
    case.loose["flood_total_lines"] = len(lines)
    return case.to_dict()


def build_limits() -> dict[str, Any]:
    case = Case(
        "limits",
        note="预算用例：3MiB 单成员（默认预算内 succeeded / 1MiB 预算下被牺牲）+ 50 成员归档",
    )
    _reset_dir(case.dir)
    huge_lines = [
        f"{_noise(i)} payload={'x' * 40} id={i:06d}"
        for i in range(LIMITS_HUGE_MEMBER_BYTES // 64)
    ]
    good_lines = _rule_lines(errors=2, plain=8)
    _write_zip(
        case.dir / "big_member.zip",
        [("huge.log", _join(huge_lines)), ("good.log", _join(good_lines))],
    )
    case.member("big_member.zip", "huge.log", huge_lines)
    case.member("big_member.zip", "good.log", good_lines)
    member_lines = {f"m{n:02d}.log": [f"ERROR member {n}"] for n in range(1, 51)}
    _write_zip(
        case.dir / "many_members.zip",
        [(name, _join(lines)) for name, lines in member_lines.items()],
    )
    for name, lines in member_lines.items():
        case.member("many_members.zip", name, lines)
    case.loose["many_members_count"] = len(member_lines)
    case.loose["huge_member_bytes"] = len(_join(huge_lines))
    return case.to_dict()


def build_longline() -> dict[str, Any]:
    """长行截断用例：raw_line >4096 UTF-8 字节被截且 raw_truncated=true.

    覆盖三个语义（契约 e2e/refs/api-contract.md §7 末；实现
    service/src/application/worker/processor.py:29,81-86,302-305）：
    1. 关键词在截断点之后的超长行：匹配在截断前对整行求值，error_kw 仍命中，
       但入库 raw_line 已被截、不再含 "ERROR"；
    2. 4096/4097 字节边界：恰好 4096 不截（<=4096），4097 截到恰好 4096；
    3. 口径是 UTF-8 字节不是字符数：切口落在一个 3 字节汉字中间时，
       errors="ignore" 丢弃残缺字节序，截断结果是 4095 字节而非 4096。
    期望值全部由 _expected_raw_line/_line_matches 对生成的行现算，不手抄。
    """
    case = Case("longline", note="长行截断语义：>4096 UTF-8 字节的命中行 raw_truncated=true")
    _reset_dir(case.dir)

    # L1：9000 字节，"ERROR" 位于字节偏移 5985（截断点之后），截断后恰好 4096 字节。
    kw1 = "ERROR longline keyword-after-cut seq=1"
    giant = "a" * 5985 + kw1 + "z" * (9000 - 5985 - len(kw1))
    assert len(giant.encode("utf-8")) == 9000

    # L2：边界行，恰好 4096 UTF-8 字节 —— 必须不截断。
    kw2 = "ERROR longline boundary-4096 seq=2 "
    at_4096 = kw2 + "b" * (4096 - len(kw2))
    assert len(at_4096.encode("utf-8")) == 4096

    # L3：边界行，恰好 4097 ASCII 字节 —— 多 1 字节即截断到恰好 4096。
    kw3 = "ERROR longline boundary-4097 seq=3 "
    at_4097 = kw3 + "c" * (4097 - len(kw3))
    assert len(at_4097.encode("utf-8")) == 4097

    # L4：5200 字节，第 4096 个字节处横插一个 3 字节汉字"错"（E9 94 99 跨越
    # 切口），ERROR 在字节偏移 4098（截断点之后）——截断后应为 4095 字节。
    kw4 = "ERROR longline cjk-cut seq=4"
    cjk_cut = "a" * 4095 + "错" + kw4 + "t" * (5200 - 4095 - len("错".encode()) - len(kw4))
    assert len(cjk_cut.encode("utf-8")) == 5200
    assert len("a".encode("utf-8") * 4095 + "错".encode("utf-8")) == 4098  # 汉字确实跨切口

    # L5：正常短行，不命中任何规则 —— 只计行数，不产生命中行。
    short = "INFO heartbeat longline ok seq=5"

    lines = [giant, at_4096, at_4097, cjk_cut, short]
    _write_zip(case.dir / "long.zip", [("long.log", _join(lines))])
    case.member("long.zip", "long.log", lines)

    per_line: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        raw, truncated = _expected_raw_line(line)
        entry: dict[str, Any] = {
            "line_no": index,
            "member_path": "long.log",
            "utf8_bytes": len(line.encode("utf-8")),
            "expected_raw_truncated": truncated,
            "expected_raw_utf8_bytes": len(raw.encode("utf-8")),
            "expected_rule_hits": sorted(_line_matches(line, case.rules)),
        }
        keyword_offset = line.lower().find("error")
        if keyword_offset >= 0:
            entry["keyword_utf8_offset"] = len(line[:keyword_offset].encode("utf-8"))
            entry["keyword_after_cut"] = entry["keyword_utf8_offset"] > RAW_LINE_LIMIT_BYTES
            entry["expected_raw_contains_error"] = "error" in raw.lower()
        per_line.append(entry)
    case.loose["longline_expect"] = {
        "archive": "long.zip",
        "member": "long.log",
        "raw_line_limit_bytes": RAW_LINE_LIMIT_BYTES,
        "semantics": "匹配在截断前对整行求值；截断按 UTF-8 字节、errors=ignore 丢残缺字符；"
                     "keyword_after_cut 的行期望 raw_truncated=true 且命中仍成立、截断后的 raw_line 不含 ERROR",
        "lines": per_line,
    }
    return case.to_dict()


def build_cancel() -> dict[str, Any]:
    case = Case("cancel", note="约 60MB 存储型 zip：运行中取消，终态必须 cancelled", expect_total_files=1)
    _reset_dir(case.dir)
    # 256 字节平移表把随机字节压成小写字母+数字：既不产生伪 ERROR/elapsed=
    # 之类的意外命中，也让 60MB 语料完全可复现（固定种子）。
    alphabet = b"abcdefghijklmnopqrstuvwxyz0123456789"
    table = bytes(alphabet[index % len(alphabet)] for index in range(256))
    rng = random.Random(20261029)
    block = rng.randbytes(1024 * 1024).translate(table)
    filler = b"\n".join(block[index : index + 2048] for index in range(0, len(block), 2048))
    per_chunk = len(filler) + 40
    chunks = [
        f"ERROR cancel-anchor {index}\nINFO bulk seq={index} ".encode("ascii") + filler + b"\n"
        for index in range(CANCEL_TARGET_BYTES // per_chunk)
    ]
    payload = b"".join(chunks)
    _write_zip(case.dir / "big.zip", [("big.log", payload)], stored=True)
    case.loose["approx_bytes"] = len(payload)
    case.loose["generated_chunks"] = len(chunks)
    case.loose["archive"] = "big.zip"
    case.loose["member"] = "big.log"
    case.loose["assert"] = "运行中取消 -> job/task 终态 cancelled，SSE 终态 cancelled"
    return case.to_dict()


def build_perf() -> tuple[dict[str, Any], dict[str, Any]]:
    """5000 规则性能用例：perf.zip + 交给 harness POST 的规则集负载."""
    case = Case("perf", rules=PERF_RULES, note="5000 关键词规则求值：正确性 + 吞吐基准")
    _reset_dir(case.dir)
    lines: list[str] = []
    for index in range(PERF_LINES):
        token = PERF_KEYWORDS[index % PERF_KEYWORD_RULE_COUNT]
        lines.append(f"INFO perf sample {index} {token} elapsed=120ms")
    dense = " ".join(PERF_KEYWORDS[:PERF_DENSE_TOKENS])
    lines.append(f"ERROR dense-line {dense}")
    for _ in range(200):
        lines.append(_noise(len(lines)))
    _write_zip(case.dir / "perf.zip", [("perf.log", _join(lines))])
    case.member("perf.zip", "perf.log", lines)
    payload = PERF_RULE_SET
    (ARTIFACTS_DIR / "perf_ruleset.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    oracle = case.to_dict()
    oracle["rule_count"] = len(PERF_RULES)
    oracle["perf_lines"] = len(lines)
    return oracle, payload


def run_mock_generator(quick: bool = False) -> dict[str, Any] | None:
    """调用仓库自带的 data/generate_mock_logs.py，把语料直接生到 /nas/mock."""
    if not MOCK_GENERATOR.exists():
        return None
    target = NAS_DIR / "mock"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    cmd = [sys.executable, str(MOCK_GENERATOR), "--out", str(target), "--seed", "20261029"]
    if quick:
        cmd.append("--quick")
    else:
        # 默认 60 包 × 10~16 成员 × 5~10 万行会有数十 GB，E2E 只需要"够多但不夸张"。
        cmd += [
            "--archives", "8", "--op-archives", "2",
            "--min-lines", "3000", "--max-lines", "6000", "--workers", "4",
        ]
    subprocess.run(cmd, check=True, cwd=str(REPO_DIR))
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "archives": len(manifest.get("archives", [])),
        "total_lines": manifest.get("total_lines"),
        "global_keyword_counts": manifest.get("global_keyword_counts"),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }


BUILDERS: dict[str, Any] = {
    "clean": build_clean,
    "formats": build_formats,
    "partial": build_partial,
    "truncated": build_truncated,
    "empty": build_empty,
    "misc": build_misc,
    "deep": build_deep,
    "encoding": build_encoding,
    "traversal": build_traversal,
    "modified": build_modified,
    "flood": build_flood,
    "limits": build_limits,
    "longline": build_longline,
    "cancel": build_cancel,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", help="只重建指定用例目录（oracle 仍整体重写）")
    parser.add_argument("--skip-mock", action="store_true", help="跳过 mock 语料生成")
    parser.add_argument("--quick-mock", action="store_true", help="mock 语料用 --quick")
    parser.add_argument("--skip-cancel", action="store_true", help="跳过 60MB 取消用例")
    args = parser.parse_args()

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    NAS_DIR.mkdir(parents=True, exist_ok=True)

    cases: dict[str, Any] = {}
    existing_path = ARTIFACTS_DIR / "fixtures.json"
    if existing_path.exists():
        cases = json.loads(existing_path.read_text(encoding="utf-8")).get("cases", {})

    selected = args.only or list(BUILDERS)
    for name in selected:
        builder = BUILDERS[name]
        if args.skip_cancel and name == "cancel":
            continue
        cases[name] = builder()
        print(f"[nas] {name}: files={cases[name]['expect_total_files']} "
              f"lines={cases[name]['expect_total_lines']} "
              f"matches={cases[name]['expect_total_matches']}")

    perf_oracle, _payload = build_perf()
    if not args.only or "perf" in args.only:
        cases["perf"] = perf_oracle
        print(f"[nas] perf: rules={perf_oracle['rule_count']} lines={perf_oracle['perf_lines']} "
              f"matches={perf_oracle['expect_total_matches']}")

    mock_summary = None
    if not args.skip_mock and not args.only:
        mock_summary = run_mock_generator(quick=args.quick_mock)
        if mock_summary:
            cases.setdefault("mock", {})["source"] = "/nas/mock"
            cases["mock"].update(
                {
                    "mode": "manifest-reconciliation",
                    "note": "命中数与 data/generate_mock_logs.py 的 manifest 对齐，不由本文件预算",
                    **mock_summary,
                }
            )
            print(f"[nas] mock: {mock_summary}")

    canonical_path = ARTIFACTS_DIR / "canonical_ruleset.json"
    canonical_path.write_text(
        json.dumps(CANONICAL_RULE_SET, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    fixtures = {
        "nas_root": str(NAS_DIR),
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "cases": cases,
        "mock": mock_summary,
    }
    ARTIFACTS_DIR.joinpath("fixtures.json").write_text(
        json.dumps(fixtures, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    total_mb = sum(path.stat().st_size for path in NAS_DIR.rglob("*") if path.is_file()) / 1e6
    print(f"[nas] total size {total_mb:.1f} MB -> {ARTIFACTS_DIR / 'fixtures.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
