"""LogHarvest 模拟数据生成器。

生成一批 ZIP 压缩日志包，供 LogHarvest 全流程（扫描 → 领取 → 复制 →
解压 → 规则匹配 → 入库 → 汇总）验证使用。

数据形态（对齐 service/src/domain/archive/reader.py 与规则引擎能力）：
- 压缩包命名：APPLOG001.ZIP ... / OPLOG001.ZIP ...（后缀大小写均可被扫描器识别）
- 每个压缩包内含多种类型的文本日志成员（app / op / sys / audit），
  成员名以 .log 结尾，可被 file_glob（默认 ``**/*``）匹配
- 日志行为 C 语言风格：
  ``[2026-10-27 10:00:00.123][INFO][pid=2048][tid=2100][app_svc.c:312] message``
- 行内埋入规则引擎可命中的内容：
  * 关键字：ERROR / WARN / FATAL / connection refused / timeout /
    segmentation fault / out of memory / auth failed 等
  * 数值字段（key=value）：cost_ms / retry / usage / rows 等，
    可被 regex 命名分组 + threshold 规则提取和过滤
- 每个文件内随机安排 1~3 个"故障爆发窗口"，窗口内错误率骤升，
  便于验证命中结果在时间轴上的聚集分析

生成完成后在输出目录写 manifest.json，记录每个包/成员的行数与关键字
命中数，作为系统分析结果的期望值对账依据。

用法示例：
    python data/generate_mock_logs.py                    # 全量：50 个 ZIP
    python data/generate_mock_logs.py --quick            # 冒烟：4 个小 ZIP
    python data/generate_mock_logs.py --archives 10 --workers 4
    python data/generate_mock_logs.py --out E:/nas_mock/logs --seed 42

仅依赖标准库，Python 3.10+。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量：日志素材
# ---------------------------------------------------------------------------

LEVELS = ("DEBUG", "INFO ", "WARN ", "ERROR", "FATAL")
# 正常时段的级别权重；爆发窗口内单独用 ERROR_LEVEL_WEIGHTS
LEVEL_WEIGHTS = (5, 85, 6, 3.5, 0.5)
ERROR_LEVEL_WEIGHTS = (0, 20, 25, 45, 10)

APP_URIS = (
    "/api/v1/orders", "/api/v1/users", "/api/v1/login", "/api/v1/payments",
    "/api/v1/inventory", "/api/v1/reports", "/api/v1/config", "/api/v1/health",
    "/api/v1/upload", "/api/v1/search",
)
APP_METHODS = ("GET", "POST", "PUT", "DELETE")
APP_MODULES = ("app_main.c", "app_service.c", "http_handler.c", "db_pool.c", "cache_mgr.c")
APP_SQL_IDS = ("sql_user_get", "sql_order_list", "sql_pay_insert", "sql_stat_agg", "sql_cfg_upd")
APP_HOSTS = ("10.20.30.11", "10.20.30.12", "db-master.internal", "cache-01.internal", "mq-broker-02")

OP_OPERATORS = ("admin", "ops_zhang", "ops_li", "auditor_wang", "svc_cron", "dev_chen")
OP_ACTIONS = (
    "LOGIN", "LOGOUT", "CREATE_USER", "DELETE_USER", "MODIFY_CONFIG",
    "RESTART_SERVICE", "EXPORT_DATA", "GRANT_ROLE", "REVOKE_ROLE", "DELETE_FILE",
)
OP_TARGETS = ("192.168.1.10", "192.168.1.25", "user:10086", "svc:logharvest", "cfg:worker.yaml", "file:/data/report.csv")
OP_REASONS = ("BAD_PASSWORD", "USER_LOCKED", "PERMISSION_DENIED", "TOKEN_EXPIRED", "TARGET_BUSY")

SYS_COMPONENTS = ("kernel", "sshd", "systemd", "crond", "ntpd", "dockerd", "kubelet")
AUDIT_EVENTS = ("SESSION_OPEN", "SESSION_CLOSE", "PRIV_ESCALATE", "CRED_ACQUIRED", "POLICY_RELOAD")

HEX_DIGITS = "0123456789abcdef"

# manifest 中统计的关键字（与规则引擎 keyword 规则的候选词对齐）
TRACKED_KEYWORDS = (
    "ERROR", "FATAL", "WARN",
    "connection refused", "timeout", "segmentation fault",
    "out of memory", "disk full", "auth failed", "core dumped",
)


def _hex_addr(rng: random.Random) -> str:
    return "0x" + "".join(rng.choice(HEX_DIGITS) for _ in range(12))


def _ip(rng: random.Random) -> str:
    return f"10.{rng.randrange(0, 256)}.{rng.randrange(0, 256)}.{rng.randrange(1, 255)}"


# ---------------------------------------------------------------------------
# 每类日志的消息模板
# ---------------------------------------------------------------------------

class MessageBuilder:
    """按日志类型生成消息体，同时返回该行命中的跟踪关键字集合。"""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def app(self, level: str) -> tuple[str, tuple[str, ...]]:
        rng = self._rng
        level = level.strip()
        if level == "INFO":
            pick = rng.randrange(4)
            if pick == 0:
                return (
                    f"request handled uri={rng.choice(APP_URIS)} method={rng.choice(APP_METHODS)} "
                    f"user_id={rng.randrange(10000, 99999)} cost_ms={rng.randrange(1, 800)} "
                    f"status={rng.choice((200, 200, 200, 201, 204, 304))}",
                    (),
                )
            if pick == 1:
                return (
                    f"db query executed sql_id={rng.choice(APP_SQL_IDS)} "
                    f"rows={rng.randrange(0, 5000)} cost_ms={rng.randrange(1, 500)}",
                    (),
                )
            if pick == 2:
                return (
                    f"cache {'hit' if rng.random() < 0.8 else 'miss'} "
                    f"key=user:{rng.randrange(10000, 99999)} ttl={rng.randrange(30, 3600)}",
                    (),
                )
            return (
                f"heartbeat ok uptime_s={rng.randrange(3600, 8640000)} "
                f"mem_usage_mb={rng.randrange(128, 1024)} conns={rng.randrange(1, 400)}",
                (),
            )
        if level == "DEBUG":
            return (
                f"trace span_id={_hex_addr(rng)[-8:]} phase="
                f"{rng.choice(('parse', 'validate', 'dispatch', 'serialize'))} "
                f"elapsed_us={rng.randrange(10, 50000)}",
                (),
            )
        if level == "WARN":
            pick = rng.randrange(3)
            if pick == 0:
                return (
                    f"slow request uri={rng.choice(APP_URIS)} "
                    f"cost_ms={rng.randrange(3000, 15000)} threshold_ms=3000",
                    (),
                )
            if pick == 1:
                return (
                    f"retry scheduled target={rng.choice(APP_HOSTS)} "
                    f"retry={rng.randrange(1, 6)} backoff_ms={rng.randrange(100, 5000)}",
                    (),
                )
            return (
                f"pool near limit active={rng.randrange(90, 100)} max=100 "
                f"usage={rng.randrange(90, 100)}",
                (),
            )
        if level == "ERROR":
            pick = rng.randrange(5)
            if pick == 0:
                return (
                    f"connection refused host={rng.choice(APP_HOSTS)} "
                    f"port={rng.choice((3306, 5432, 6379, 8080, 9092))} retry={rng.randrange(1, 10)}",
                    ("ERROR", "connection refused"),
                )
            if pick == 1:
                return (
                    f"request timeout uri={rng.choice(APP_URIS)} "
                    f"cost_ms={rng.randrange(5000, 60000)} threshold_ms=5000",
                    ("ERROR", "timeout"),
                )
            if pick == 2:
                return (
                    f"auth failed user={rng.choice(OP_OPERATORS)} "
                    f"reason={rng.choice(OP_REASONS)} ip={_ip(rng)}",
                    ("ERROR", "auth failed"),
                )
            if pick == 3:
                return (
                    f"db query failed sql_id={rng.choice(APP_SQL_IDS)} "
                    f"errno={rng.randrange(1000, 1200)} cost_ms={rng.randrange(1, 9000)}",
                    ("ERROR",),
                )
            return (
                f"upstream error host={rng.choice(APP_HOSTS)} status="
                f"{rng.choice((500, 502, 503, 504))} cost_ms={rng.randrange(1, 30000)}",
                ("ERROR",),
            )
        # FATAL
        pick = rng.randrange(4)
        if pick == 0:
            return (
                f"segmentation fault addr={_hex_addr(rng)} "
                f"module={rng.choice(APP_MODULES)} signal=11",
                ("FATAL", "segmentation fault"),
            )
        if pick == 1:
            return (
                f"out of memory requested_mb={rng.randrange(1024, 8192)} "
                f"available_mb={rng.randrange(0, 256)}",
                ("FATAL", "out of memory"),
            )
        if pick == 2:
            return (
                f"disk full path=/data/logs usage={rng.randrange(98, 101)} "
                f"free_mb={rng.randrange(0, 50)}",
                ("FATAL", "disk full"),
            )
        return (
            f"core dumped signal={rng.choice((6, 11))} pid={rng.randrange(1000, 65000)} "
            f"module={rng.choice(APP_MODULES)}",
            ("FATAL", "core dumped"),
        )

    def op(self, level: str) -> tuple[str, tuple[str, ...]]:
        rng = self._rng
        level = level.strip()
        operator = rng.choice(OP_OPERATORS)
        action = rng.choice(OP_ACTIONS)
        target = rng.choice(OP_TARGETS)
        if level in ("INFO", "DEBUG"):
            return (
                f"operator={operator} action={action} target={target} "
                f"result=SUCCESS cost_ms={rng.randrange(1, 3000)} ip={_ip(rng)}",
                (),
            )
        if level == "WARN":
            return (
                f"operator={operator} action={action} target={target} "
                f"result=RETRY reason=TARGET_BUSY cost_ms={rng.randrange(1000, 9000)}",
                (),
            )
        return (  # ERROR / FATAL
            f"operator={operator} action={action} target={target} "
            f"result=FAILED reason={rng.choice(OP_REASONS)} ip={_ip(rng)}",
            (level, "auth failed") if rng.random() < 0.3 else (level,),
        )

    def sys(self, level: str) -> tuple[str, tuple[str, ...]]:
        rng = self._rng
        level = level.strip()
        comp = rng.choice(SYS_COMPONENTS)
        if level in ("INFO", "DEBUG"):
            return (
                f"{comp}: routine check ok load={rng.randrange(1, 200) / 100:.2f} "
                f"mem_usage_mb={rng.randrange(256, 4096)}",
                (),
            )
        if level == "WARN":
            return (
                f"{comp}: resource pressure cpu={rng.randrange(80, 96)} "
                f"usage={rng.randrange(80, 96)}",
                (),
            )
        if level == "ERROR":
            return (
                f"{comp}: connection refused to peer {_ip(rng)}:{rng.randrange(1024, 65535)} "
                f"retry={rng.randrange(1, 8)}",
                ("ERROR", "connection refused"),
            )
        return (
            f"{comp}: out of memory killer invoked victim_pid={rng.randrange(1000, 65000)} "
            f"requested_mb={rng.randrange(512, 8192)}",
            ("FATAL", "out of memory"),
        )

    def audit(self, level: str) -> tuple[str, tuple[str, ...]]:
        rng = self._rng
        level = level.strip()
        event = rng.choice(AUDIT_EVENTS)
        base = (
            f"audit event={event} uid={rng.randrange(0, 65000)} "
            f"ses={rng.randrange(1, 500)} pid={rng.randrange(1000, 65000)} "
            f"res={'success' if level in ('INFO', 'DEBUG') else 'fail'}"
        )
        if level in ("ERROR", "FATAL"):
            return base, (level, "auth failed")
        return base, ()


def _build_message(builder: MessageBuilder, kind: str, level: str) -> tuple[str, tuple[str, ...]]:
    if kind == "app":
        return builder.app(level)
    if kind == "op":
        return builder.op(level)
    if kind == "sys":
        return builder.sys(level)
    return builder.audit(level)


# ---------------------------------------------------------------------------
# 日志成员生成
# ---------------------------------------------------------------------------

@dataclass
class MemberResult:
    name: str
    lines: int
    uncompressed_bytes: int
    keyword_counts: dict[str, int] = field(default_factory=dict)


class LogFileWriter:
    """把一个日志成员直接流式写进打开的 ZipFile，不落中间文件。"""

    CHUNK_LINES = 5000

    def __init__(self, zf: zipfile.ZipFile, member_name: str, rng: random.Random,
                 kind: str, start_time: datetime, total_lines: int) -> None:
        self._zf = zf
        self._member_name = member_name
        self._rng = rng
        self._kind = kind
        self._builder = MessageBuilder(rng)
        self._ts = start_time
        self._total_lines = total_lines
        self._keyword_counts: dict[str, int] = {k: 0 for k in TRACKED_KEYWORDS}
        self._lines = 0
        self._bytes = 0
        # 故障爆发窗口：[起, 止) 行号区间
        self._bursts = self._plan_bursts()

    def _plan_bursts(self) -> list[tuple[int, int]]:
        rng = self._rng
        count = rng.randrange(1, 4)
        bursts: list[tuple[int, int]] = []
        for _ in range(count):
            start = rng.randrange(int(self._total_lines * 0.1), int(self._total_lines * 0.9))
            length = rng.randrange(200, 2000)
            bursts.append((start, min(start + length, self._total_lines)))
        return bursts

    def _in_burst(self, line_idx: int) -> bool:
        return any(start <= line_idx < end for start, end in self._bursts)

    def _pick_level(self, in_burst: bool) -> str:
        weights = ERROR_LEVEL_WEIGHTS if in_burst else LEVEL_WEIGHTS
        return self._rng.choices(LEVELS, weights=weights, k=1)[0]

    def _next_ts(self) -> str:
        # 时间单调推进 0~500ms；每推进一天回绕到当天 00:00，保持日志跨数天
        self._ts += timedelta(milliseconds=self._rng.randrange(0, 501))
        if self._ts.hour == 0 and self._ts.minute == 0 and self._rng.random() < 0.0005:
            self._ts = self._ts.replace(hour=0, minute=0, second=0, microsecond=0)
        return self._ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{self._ts.microsecond // 1000:03d}"

    def write(self) -> MemberResult:
        pid = self._rng.randrange(1000, 65000)
        src_file = {"app": "app_svc.c", "op": "op_audit.c", "sys": "sys_mon.c", "audit": "audit_log.c"}[self._kind]
        counts = self._keyword_counts
        chunk: list[str] = []
        # 关键：zf.open() 用的是 ZipInfo 自带的 compress_type，构造函数里的
        # compression=ZIP_DEFLATED 对预构造的 ZipInfo 无效。不显式设置会退化成
        # ZIP_STORED（不压缩，压缩比 1:1），既浪费 ~10 倍磁盘，也让 Worker 的
        # 解压路径退化成直通、无法真正验证流式解压。
        info = zipfile.ZipInfo(self._member_name, date_time=self._ts.timetuple()[:6])
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16  # 普通文件权限位
        with self._zf.open(info, "w", force_zip64=True) as stream:
            for idx in range(self._total_lines):
                level = self._pick_level(self._in_burst(idx))
                message, hits = _build_message(self._builder, self._kind, level)
                for kw in hits:
                    counts[kw] += 1
                line = (
                    f"[{self._next_ts()}][{level}][pid={pid}]"
                    f"[tid={pid + self._rng.randrange(1, 64)}][{src_file}:"
                    f"{self._rng.randrange(20, 900)}] {message}\n"
                )
                chunk.append(line)
                if len(chunk) >= self.CHUNK_LINES:
                    self._flush(stream, chunk)
                    chunk.clear()
            if chunk:
                self._flush(stream, chunk)
        return MemberResult(
            name=self._member_name,
            lines=self._lines,
            uncompressed_bytes=self._bytes,
            keyword_counts={k: v for k, v in counts.items() if v},
        )

    def _flush(self, stream, chunk: list[str]) -> None:
        payload = "".join(chunk).encode("utf-8")
        stream.write(payload)
        self._lines += len(chunk)
        self._bytes += len(payload)


# ---------------------------------------------------------------------------
# 压缩包规格与生成
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArchiveSpec:
    name: str              # 例如 APPLOG001.ZIP
    category: str          # app | op
    index: int             # 包序号，参与随机种子
    member_count: int
    line_range: tuple[int, int]
    base_seed: int
    start_time: datetime


# 每种压缩包内成员的日志类型构成：主类型 + 混入的其他类型，
# 对应需求「一个压缩文件中有多种日志」。
CATEGORY_MIX = {
    "app": ("app", "app", "app", "app", "app", "sys", "audit", "op"),
    "op": ("op", "op", "op", "op", "op", "audit", "app", "sys"),
}


def build_specs(args: argparse.Namespace) -> list[ArchiveSpec]:
    specs: list[ArchiveSpec] = []
    app_count = args.archives - args.op_archives
    plan = [("app", i + 1) for i in range(app_count)] + [("op", i + 1) for i in range(args.op_archives)]
    for seq, (category, idx) in enumerate(plan):
        prefix = "APPLOG" if category == "app" else "OPLOG"
        specs.append(
            ArchiveSpec(
                name=f"{prefix}{idx:03d}.ZIP",
                category=category,
                index=seq,
                member_count=args.member_count if args.member_count else 0,  # 0 → 每包随机
                line_range=(args.min_lines, args.max_lines),
                base_seed=args.seed + seq * 100_000,
                # 每个包起始时间错开，模拟不同机器/日期归档
                start_time=datetime(2026, 10, 20, 0, 0, 0) + timedelta(days=seq % 7, hours=(seq * 3) % 24),
            )
        )
    return specs


def generate_archive(spec: ArchiveSpec, out_dir: Path, args_ns: tuple[int, int, int, int]) -> dict:
    """在子进程中生成一个 ZIP。args_ns = (min_files, max_files, min_lines, max_lines)。"""
    min_files, max_files, min_lines, max_lines = args_ns
    rng = random.Random(spec.base_seed)
    member_count = spec.member_count or rng.randrange(min_files, max_files + 1)
    mix = CATEGORY_MIX[spec.category]
    path = out_dir / spec.name
    tmp_path = path.with_suffix(".part")

    members: list[dict] = []
    totals: dict[str, int] = {k: 0 for k in TRACKED_KEYWORDS}
    total_lines = 0
    started = time.monotonic()
    # deflate level=1：文本日志压缩比仍约 8~12:1，速度比默认级别快 3~5 倍
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for member_idx in range(member_count):
            kind = mix[member_idx % len(mix)]
            lines = rng.randrange(min_lines, max_lines + 1)
            member_seed = spec.base_seed + (member_idx + 1) * 7919
            name = f"{kind}_{spec.start_time:%Y%m%d}_{member_idx + 1:02d}.log"
            writer = LogFileWriter(
                zf, name, random.Random(member_seed), kind,
                spec.start_time + timedelta(hours=member_idx), lines,
            )
            result = writer.write()
            members.append(
                {
                    "name": result.name,
                    "kind": kind,
                    "lines": result.lines,
                    "uncompressed_bytes": result.uncompressed_bytes,
                    "keyword_counts": result.keyword_counts,
                }
            )
            total_lines += result.lines
            for kw, cnt in result.keyword_counts.items():
                totals[kw] += cnt
    tmp_path.replace(path)
    return {
        "archive": spec.name,
        "category": spec.category,
        "members": members,
        "member_count": member_count,
        "total_lines": total_lines,
        "keyword_counts": {k: v for k, v in totals.items() if v},
        "zip_bytes": path.stat().st_size,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


def _worker(task: tuple[ArchiveSpec, Path, tuple[int, int, int, int]]) -> dict:
    spec, out_dir, bounds = task
    return generate_archive(spec, out_dir, bounds)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 LogHarvest 模拟压缩日志数据")
    parser.add_argument("--out", default=str(Path(__file__).parent / "logs"),
                        help="输出目录（默认 data/logs）")
    parser.add_argument("--archives", type=int, default=50, help="压缩包总数（默认 50）")
    parser.add_argument("--op-archives", type=int, default=10,
                        help="其中 OPLOG 包数量，其余为 APPLOG（默认 10）")
    parser.add_argument("--min-files", type=int, default=10, help="每包最少成员数（默认 10）")
    parser.add_argument("--max-files", type=int, default=16, help="每包最多成员数（默认 16）")
    parser.add_argument("--member-count", type=int, default=0,
                        help="固定每包成员数（0=在 min/max-files 间随机）")
    parser.add_argument("--min-lines", type=int, default=50_000, help="每成员最少行数（默认 5 万）")
    parser.add_argument("--max-lines", type=int, default=100_000, help="每成员最多行数（默认 10 万）")
    parser.add_argument("--seed", type=int, default=20261027, help="随机种子（默认 20261027，可复现）")
    parser.add_argument("--workers", type=int, default=0,
                        help="并行进程数（默认 min(CPU 核数, 8)）")
    parser.add_argument("--quick", action="store_true",
                        help="冒烟模式：4 个包 × 10 成员 × 1000~2000 行，几秒完成")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.quick:
        args.archives, args.op_archives = 3, 1
        args.member_count = 10
        args.min_lines, args.max_lines = 1_000, 2_000
    if args.op_archives >= args.archives:
        print("--op-archives 必须小于 --archives", file=sys.stderr)
        return 2
    if args.min_lines > args.max_lines:
        print("--min-lines 不能大于 --max-lines", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    specs = build_specs(args)
    workers = args.workers or min(len(specs), os.cpu_count() or 4, 8)
    bounds = (args.min_files, args.max_files, args.min_lines, args.max_lines)

    est_lines = args.archives * (args.member_count or (args.min_files + args.max_files) / 2) \
        * (args.min_lines + args.max_lines) / 2
    print(f"输出目录: {out_dir.resolve()}")
    print(f"压缩包: {args.archives} 个（APPLOG {args.archives - args.op_archives} + OPLOG {args.op_archives}）")
    print(f"每包成员: {args.member_count or f'{args.min_files}~{args.max_files}'} 个，"
          f"每成员 {args.min_lines}~{args.max_lines} 行")
    print(f"预计总行数: ~{est_lines / 1e6:.1f}M，解压后 ~{est_lines * 115 / 1e9:.1f} GB（ZIP 后约 1/10）")
    print(f"并行进程: {workers}，种子: {args.seed}")

    started = time.monotonic()
    results: list[dict] = []
    tasks = [(spec, out_dir, bounds) for spec in specs]
    if workers <= 1:
        for i, task in enumerate(tasks, 1):
            results.append(_worker(task))
            print(f"  [{i}/{len(tasks)}] {results[-1]['archive']} 完成（{results[-1]['elapsed_s']}s）")
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_worker, task): task[0].name for task in tasks}
            done = 0
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                done += 1
                print(f"  [{done}/{len(tasks)}] {result['archive']} 完成（{result['elapsed_s']}s）")

    results.sort(key=lambda r: r["archive"])
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "seed": args.seed,
        "args": {k: v for k, v in vars(args).items() if k != "out"},
        "archive_count": len(results),
        "total_lines": sum(r["total_lines"] for r in results),
        "total_zip_bytes": sum(r["zip_bytes"] for r in results),
        "global_keyword_counts": {
            kw: sum(r["keyword_counts"].get(kw, 0) for r in results) for kw in TRACKED_KEYWORDS
        },
        "archives": results,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    elapsed = time.monotonic() - started
    print(f"\n全部完成：{len(results)} 个 ZIP，共 {manifest['total_lines']:,} 行，"
          f"压缩后 {manifest['total_zip_bytes'] / 1e6:.0f} MB，耗时 {elapsed:.0f}s")
    print(f"期望命中统计已写入: {manifest_path}")
    print("全局关键字命中数（供规则集对账）:")
    for kw, cnt in manifest["global_keyword_counts"].items():
        if cnt:
            print(f"  {kw!r}: {cnt:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
