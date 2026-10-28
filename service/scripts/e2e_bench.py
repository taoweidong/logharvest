"""全流程 E2E 性能基准：任务下发 → Worker 领取 → 日志分析处理 → 数据入库。

用途（非测试套件，性能分析工具）：
    python -m scripts.e2e_bench --pg postgresql+psycopg://user:pass@127.0.0.1:5433/postgres \
        --label baseline --archives 20 --member-count 3 --max-lines 10000 --min-lines 10000 \
        --rules 50 --workers 2

流程：
  1. 在同一 PG 实例上创建独立基准库（logharvest_bench_<label>，结束默认删除）；
  2. 通过真实 HTTP API（TestClient 走完整 ASGI 栈）登录、建数据源/规则集/作业；
  3. 数据由 data/generate_mock_logs.py（固定种子）生成，可跨运行复用以对比优化前后；
  4. 启动 K 个真实 Worker 子进程（python -m src.worker，生产入口）消费队列；
  5. 作业终态后采集分阶段耗时与命中统计；
  6. 正确性 Oracle：a) 朴素求值器（无预筛）独立重放首个成员前 1000 行，
     与库中 (member_path, line_no, rule_id) 三元组逐一比对；
     b) 生成器 manifest 的关键字对账（行级上限口径）；
  7. 输出 JSON 报告（--report），供优化前后对比。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVICE = REPO / "service"
DATA_GENERATOR = REPO / "data" / "generate_mock_logs.py"

MATCH_SUBSET_LINES = 1000  # Oracle 子集：首个成员前 N 行


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pg", required=True, help="PG 连接串（postgresql+psycopg://user:pass@host:port/postgres）"
    )
    parser.add_argument("--label", required=True, help="运行标签（进入库名与报告文件名）")
    parser.add_argument("--archives", type=int, default=20)
    parser.add_argument("--member-count", type=int, default=3, help="每包成员数（0=随机 10-16）")
    parser.add_argument("--min-lines", type=int, default=10000)
    parser.add_argument("--max-lines", type=int, default=10000)
    parser.add_argument("--op-archives", type=int, default=0)
    parser.add_argument("--rules", type=int, default=50, help="规则总数（含 5k 极限场景）")
    parser.add_argument("--workers", type=int, default=2, help="Worker 子进程数")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--data-dir", default="", help="复用已生成的数据目录（跳过生成）")
    parser.add_argument("--timeout", type=int, default=3600, help="作业终态等待上限（秒）")
    parser.add_argument("--keep-db", action="store_true", help="结束后保留基准库（调试用）")
    parser.add_argument(
        "--baseline-report",
        default="",
        help="基线报告 JSON：比对命中总数与逐规则计数（跨运行正确性 Oracle）",
    )
    parser.add_argument("--report", default="", help="报告 JSON 输出路径")
    return parser.parse_args()


def build_rules(count: int, seed: int) -> dict:
    """构造 R 条规则：少量高命中 + 大量不命中（含 any/all、有无字面量前缀的正则）。"""
    from src.domain.rules.models import KeywordMatch, RegexMatch, Rule

    rng = random.Random(seed)
    rules: list[Rule] = []

    n_hit_kw = max(1, round(count * 0.002))
    n_hit_re = max(1, round(count * 0.003))
    n_plain_kw = round(count * 0.4)
    n_plain_re = count - n_hit_kw - n_hit_re - n_plain_kw
    n_plain_re = max(n_plain_re, 0)

    # 高命中关键字（与生成器消息体对齐，大小写敏感，便于对账）
    hit_kws = (
        "ERROR",
        "FATAL",
        "WARN",
        "timeout",
        "connection refused",
        "out of memory",
        "auth failed",
        "core dumped",
    )
    rules.extend(
        Rule(
            id=f"kw-hit-{i:04d}",
            match=KeywordMatch(
                type="keyword", any=[hit_kws[i % len(hit_kws)]], all=[], case_sensitive=True
            ),
        )
        for i in range(n_hit_kw)
    )
    # 高命中正则：同一字面量前缀 + 不同阈值（验证 hint 去重收益）
    re_families = (
        ("cost_ms=(?P<cost>\\d{3,6})", "cost", "gt"),
        ("usage=(?P<usage>\\d{2,3})", "usage", "gte"),
        ("rows=(?P<rows>\\d{1,5})", "rows", "lt"),
    )
    for i in range(n_hit_re):
        pattern, field, op = re_families[i % len(re_families)]
        rules.append(
            Rule(
                id=f"re-hit-{i:04d}",
                match=RegexMatch(type="regex", pattern=pattern, flags="", timeout_ms=50),
                threshold={"field": field, "op": op, "value": str(500 + i * 37)},
            )
        )
    # 不命中关键字：一半 any（大小写不敏感，压 lower 路径），一半 all 双针
    for i in range(n_plain_kw):
        if i % 2 == 0:
            rules.append(
                Rule(
                    id=f"kw-miss-{i:05d}",
                    match=KeywordMatch(
                        type="keyword", any=[f"benchkw_{i:05d}"], all=[], case_sensitive=False
                    ),
                )
            )
        else:
            rules.append(
                Rule(
                    id=f"kw-miss-{i:05d}",
                    match=KeywordMatch(
                        type="keyword",
                        any=[],
                        all=[f"benchall_{i:05d}", f"benchall2_{i:05d}"],
                        case_sensitive=False,
                    ),
                )
            )
    # 不命中正则：一半带字面量前缀（可提取 hint），一半以元字符开头（不可提取）
    for i in range(n_plain_re):
        if i % 2 == 0:
            pattern = f"bench{i:05d}=(?P<x>\\d+)"
        else:
            pattern = f"\\d{{1,3}}-benchx{i:05d}=(?P<x>\\d+)"
        rules.append(
            Rule(
                id=f"re-miss-{i:05d}",
                match=RegexMatch(type="regex", pattern=pattern, flags="", timeout_ms=50),
            )
        )

    rng.shuffle(rules)
    config = {
        "name": f"bench-rules-{count}",
        "file_glob": "**/*.log",
        "encoding": "utf-8",
        "fallback_encoding": "gb18030",
        "rules": [r.model_dump(mode="json") if hasattr(r, "model_dump") else r for r in rules],
    }
    return config


def prepare_database(admin_pg: str, db_name: str, keep: bool) -> str:
    import psycopg

    base = admin_pg.replace("+psycopg", "").rsplit("/", 1)[0]
    conn = psycopg.connect(base + "/postgres", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
            cur.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        conn.close()
    return base + "/" + db_name


def drop_database(admin_pg: str, db_name: str) -> None:
    import psycopg

    base = admin_pg.replace("+psycopg", "").rsplit("/", 1)[0]
    conn = psycopg.connect(base + "/postgres", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    finally:
        conn.close()


def generate_data(args: argparse.Namespace) -> Path:
    data_dir = (
        Path(args.data_dir)
        if args.data_dir
        else (
            REPO / "data" / "_bench" / f"a{args.archives}_m{args.member_count or 'r'}"
            f"_l{args.min_lines}-{args.max_lines}_s{args.seed}"
        )
    )
    if (data_dir / "manifest.json").exists():
        print(f"[data] 复用已生成数据：{data_dir}")
        return data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(DATA_GENERATOR),
        "--out",
        str(data_dir),
        "--archives",
        str(args.archives),
        "--op-archives",
        str(args.op_archives),
        "--min-lines",
        str(args.min_lines),
        "--max-lines",
        str(args.max_lines),
        "--seed",
        str(args.seed),
        "--workers",
        "8",
    ]
    if args.member_count:
        cmd += ["--member-count", str(args.member_count)]
    print(f"[data] 生成模拟数据：{' '.join(cmd)}")
    t0 = time.perf_counter()
    subprocess.run(cmd, check=True, cwd=str(REPO))
    print(f"[data] 生成完成，用时 {time.perf_counter() - t0:.1f}s")
    return data_dir


def claim_plan_ms(bench_pg_dsn: str) -> float:
    """EXPLAIN ANALYZE 队列领取语句（只读事务内执行，结束后回滚，无副作用）。"""
    import psycopg

    claim = """
    WITH picked AS (
        SELECT t.id, d.root_path
        FROM file_tasks t
        JOIN jobs j ON j.id = t.job_id
        LEFT JOIN data_sources d ON d.id = j.data_source_id
        WHERE t.status = 'pending'
          AND t.scheduled_at <= now()
          AND j.cancel_requested = false
        ORDER BY t.scheduled_at, t.id
        LIMIT 1
        FOR UPDATE OF t SKIP LOCKED
    )
    UPDATE file_tasks t
    SET status = 'running', worker_id = 'explain', attempts = t.attempts + 1,
        started_at = COALESCE(t.started_at, now()), heartbeat_at = now()
    FROM picked p
    WHERE t.id = p.id
    RETURNING t.id
    """
    conn = psycopg.connect(bench_pg_dsn)
    try:
        # EXPLAIN ANALYZE 会真实执行该语句，但事务随后回滚：领取被撤销，
        # 仅保留计划与耗时（此时作业已终态，无运行中的 Worker 竞争）。
        rows = conn.execute("EXPLAIN ANALYZE " + claim).fetchall()
        text = "\n".join(r[0] for r in rows)
        conn.rollback()
        for line in text.splitlines():
            if "Execution Time" in line:
                return float(line.split(":")[1].strip().replace(" ms", ""))
        return -1.0
    finally:
        conn.close()


def oracle_subset(data_dir: Path, config: dict, bench_pg_dsn: str, job_id: int) -> dict:
    """朴素求值器（无任何预筛）重放首个成员前 N 行，与库中三元组逐一比对。"""
    from sqlmodel import Session, create_engine

    from src.domain.archive.reader import scan_archive
    from src.domain.rules.evaluator import compile_ruleset
    from src.domain.rules.models import RuleSetConfig

    archives = sorted(data_dir.glob("*.ZIP")) or sorted(data_dir.glob("*.zip"))
    first = archives[0]
    compiled = compile_ruleset(RuleSetConfig.model_validate(config))

    expected: set[tuple[str, int, str]] = set()
    member_seen: set[str] = set()
    current_member = None
    member_lines = 0
    scan = scan_archive(first)
    try:
        for line in scan.lines:
            if line.member_path != current_member:
                current_member = line.member_path
                member_seen.add(current_member)
                member_lines = 0
            member_lines += 1
            if member_lines > MATCH_SUBSET_LINES or len(member_seen) > 1:
                break
            for match in compile_ruleset_eval(compiled, line.text, line.line_no):
                expected.add((line.member_path, line.line_no, match.rule_id))
    finally:
        close = getattr(scan.lines, "close", None)
        if close is not None:
            close()

    # 库中同口径三元组（该作业首个文件任务，同成员，同行号范围）
    engine = create_engine(bench_pg_dsn.replace("+psycopg", ""))
    with Session(engine) as session:
        rows = session.execute(
            __import__("sqlalchemy").text(
                """
                SELECT m.member_path, m.line_no, m.rule_id
                FROM matches m
                JOIN file_tasks t ON t.id = m.file_task_id
                WHERE t.job_id = :job_id
                  AND t.source_path = :source_path
                  AND m.member_path = :member_path
                  AND m.line_no <= :limit
                """
            ),
            {
                "job_id": job_id,
                "source_path": str(first),
                "member_path": sorted(member_seen)[0],
                "limit": MATCH_SUBSET_LINES,
            },
        ).all()
    engine.dispose()
    actual = {(r[0], int(r[1]), r[2]) for r in rows}
    missing = expected - actual
    extra = actual - expected
    return {
        "subset_member": sorted(member_seen)[0] if member_seen else "",
        "expected_triples": len(expected),
        "actual_triples": len(actual),
        "missing": len(missing),
        "extra": len(extra),
        "ok": not missing and not extra,
        "sample_missing": sorted(missing)[:5],
        "sample_extra": sorted(extra)[:5],
    }


def compile_ruleset_eval(compiled, line: str, line_no: int):
    from src.domain.rules.evaluator import evaluate_line

    return evaluate_line(compiled, line, line_no)


def main() -> int:
    args = parse_args()
    label = args.label
    db_name = f"logharvest_bench_{label}"
    bench_url = prepare_database(args.pg, db_name, args.keep_db)
    bench_dsn = bench_url.replace("+psycopg", "")
    print(f"[db] 基准库：{db_name}")

    # 环境必须在导入 src 前落位（Settings 导入期校验 + engine 按它创建）
    os.environ["LOGHARVEST_DATABASE_URL"] = bench_url
    os.environ["LOGHARVEST_ENVIRONMENT"] = "test"
    os.environ["LOGHARVEST_JWT_SECRET_KEY"] = "bench-secret-key-0123456789-abcdef-0123"
    os.environ["LOGHARVEST_ADMIN_USERNAME"] = "admin"
    os.environ["LOGHARVEST_ADMIN_PASSWORD"] = "Bench-Admin-123"
    os.environ["LOGHARVEST_NAS_READ_ONLY_POLICY"] = "warn"
    os.environ["LOGHARVEST_AUTO_CREATE_TABLES"] = "true"
    batch = os.environ.get("LOGHARVEST_MATCHES_BATCH_SIZE", "1000")
    os.environ["LOGHARVEST_MATCHES_BATCH_SIZE"] = batch

    report: dict = {"label": label, "scale": vars(args), "started": datetime.now(UTC).isoformat()}

    data_dir = generate_data(args)

    from fastapi.testclient import TestClient

    from src.infrastructure import database as database_module
    from src.main import create_app

    app = create_app()
    t_lifespan = time.perf_counter()
    with TestClient(app) as client:
        print(f"[app] lifespan（迁移+种子）{time.perf_counter() - t_lifespan:.1f}s")

        from sqlmodel import Session

        from src.application.services.auth import ensure_admin

        with Session(database_module.engine) as session:
            ensure_admin(session, "admin", "Bench-Admin-123")

        resp = client.post("/api/login", json={"username": "admin", "password": "Bench-Admin-123"})
        resp.raise_for_status()
        token = resp.json()["data"]["accessToken"]
        headers = {"Authorization": f"Bearer {token}"}

        config = build_rules(args.rules, args.seed)

        t0 = time.perf_counter()
        resp = client.post(
            "/api/data-sources", headers=headers, json={"name": "bench", "root_path": str(data_dir)}
        )
        resp.raise_for_status()
        source_id = resp.json()["id"]

        resp = client.post(
            "/api/rule-sets",
            headers=headers,
            json={"name": f"bench-rules-{args.rules}", "config": config},
        )
        resp.raise_for_status()
        rule_set_id = resp.json()["id"]
        report["plan"] = {
            "data_source_s": round(time.perf_counter() - t0, 3),
        }

        t0 = time.perf_counter()
        resp = client.post(
            "/api/jobs",
            headers={**headers, "Idempotency-Key": f"bench-{label}"},
            json={
                "name": f"bench-{label}",
                "data_source_id": source_id,
                "source_path": str(data_dir),
                "rule_set_id": rule_set_id,
            },
        )
        create_s = time.perf_counter() - t0
        if resp.status_code != 201:
            print("[jobs] 创建失败：", resp.status_code, resp.text[:400])
            return 1
        job = resp.json()
        job_id = job["id"]
        report["create_job_s"] = round(create_s, 3)
        report["total_files"] = job["total_files"]
        report["total_bytes"] = job["total_bytes"]
        print(
            f"[jobs] 创建作业 #{job_id}：{job['total_files']} 个文件任务，"
            f"{job['total_bytes'] / 1e6:.1f} MB，用时 {create_s:.2f}s"
        )

        worker_log_dir = data_dir.parent / "worker-logs"
        worker_log_dir.mkdir(parents=True, exist_ok=True)
        procs = []
        t0 = time.perf_counter()
        for i in range(args.workers):
            env = {**os.environ, "LOGHARVEST_WORKER_ID": f"bench-w{i:02d}"}
            log_file = (worker_log_dir / f"{label}-w{i:02d}.log").open("wb")
            procs.append(
                (
                    subprocess.Popen(
                        [sys.executable, "-m", "src.worker"],
                        cwd=str(SERVICE),
                        env=env,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                    ),
                    log_file,
                )
            )

        deadline = time.monotonic() + args.timeout
        final_status = None
        poll_times = []
        while time.monotonic() < deadline:
            r = client.get(f"/api/jobs/{job_id}", headers=headers)
            r.raise_for_status()
            body = r.json()
            if body["status"] in ("succeeded", "partial", "failed", "cancelled"):
                final_status = body["status"]
                break
            poll_times.append(time.perf_counter())
            time.sleep(0.5)
        e2e_s = time.perf_counter() - t0
        for proc, _log_file in procs:
            proc.terminate()
        for proc, _log_file in procs:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            log_file.close()
        if final_status is None:
            print("[jobs] 超时未终态")
            report["timeout"] = True
        report["job_status"] = final_status
        report["e2e_s"] = round(e2e_s, 3)
        print(f"[e2e] 作业终态 {final_status}，全流程 {e2e_s:.1f}s")

    # 进程内连接已释放，直接用 psycopg 采集统计
    import psycopg

    conn = psycopg.connect(bench_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, count(*) FROM file_tasks WHERE job_id = %s GROUP BY status",
                (job_id,),
            )
            report["tasks_by_status"] = dict(cur.fetchall())
            cur.execute(
                "SELECT count(*) FROM matches m JOIN file_tasks t ON t.id = m.file_task_id "
                "WHERE t.job_id = %s",
                (job_id,),
            )
            report["matches_total"] = cur.fetchone()[0]
            cur.execute(
                """
                SELECT m.rule_id, count(*) FROM matches m
                JOIN file_tasks t ON t.id = m.file_task_id
                WHERE t.job_id = %s GROUP BY m.rule_id ORDER BY m.rule_id
                """,
                (job_id,),
            )
            report["matches_by_rule"] = dict(cur.fetchall())
            cur.execute(
                """
                SELECT min(t.started_at), max(t.finished_at),
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY
                           EXTRACT(EPOCH FROM (t.finished_at - t.started_at))),
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY
                           EXTRACT(EPOCH FROM (t.finished_at - t.started_at)))
                FROM file_tasks t WHERE t.job_id = %s AND t.finished_at IS NOT NULL
            """,
                (job_id,),
            )
            row = cur.fetchone()
            if row and row[0]:
                report["process_wall_s"] = round((row[1] - row[0]).total_seconds(), 2)
                report["task_duration_p50_s"] = round(float(row[2]), 3)
                report["task_duration_p95_s"] = round(float(row[3]), 3)
            cur.execute(
                """
                SELECT coalesce(sum(processed_lines), 0), coalesce(sum(matched_rows), 0)
                FROM file_tasks WHERE job_id = %s
            """,
                (job_id,),
            )
            processed, matched = cur.fetchone()
            report["processed_lines"] = int(processed)
            report["matched_rows"] = int(matched)
            if report.get("processed_lines"):
                report["throughput_lines_per_s"] = round(
                    report["processed_lines"] / max(e2e_s, 0.001), 0
                )
    finally:
        conn.close()

    report["claim_explain_ms"] = claim_plan_ms(bench_dsn)

    report["oracle_naive_subset"] = oracle_subset(data_dir, config, bench_dsn, job_id)
    if args.baseline_report:
        baseline = json.loads(Path(args.baseline_report).read_text(encoding="utf-8"))
        same_total = report["matches_total"] == baseline.get("matches_total")
        base_rules = baseline.get("matches_by_rule", {})
        only_base = {k: v for k, v in base_rules.items() if report["matches_by_rule"].get(k) != v}
        only_now = {k: v for k, v in report["matches_by_rule"].items() if base_rules.get(k) != v}
        report["baseline_consistency"] = {
            "matches_total_equal": same_total,
            "rules_only_in_baseline": only_base,
            "rules_only_in_current": only_now,
            "ok": same_total and not only_base and not only_now,
        }
        print(
            f"[oracle] 基线一致性：matches_total {baseline.get('matches_total')} → "
            f"{report['matches_total']}，逐规则差异 {len(only_base) + len(only_now)}"
        )

    database_module.engine.dispose()
    if not args.keep_db:
        drop_database(args.pg, db_name)
        print("[db] 基准库已删除")

    report_path = (
        Path(args.report) if args.report else (REPO / "data" / "_bench" / f"report-{label}.json")
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"[report] {report_path}")
    print(
        json.dumps(
            {
                k: report[k]
                for k in report
                if k
                in (
                    "create_job_s",
                    "e2e_s",
                    "job_status",
                    "total_files",
                    "matches_total",
                    "processed_lines",
                    "throughput_lines_per_s",
                    "process_wall_s",
                    "task_duration_p50_s",
                    "task_duration_p95_s",
                    "claim_explain_ms",
                )
            },
            indent=2,
        )
    )
    oracle_ok = report["oracle_naive_subset"]["ok"]
    if args.baseline_report:
        oracle_ok = oracle_ok and report["baseline_consistency"]["ok"]
    print("[oracle]", "OK" if oracle_ok else "FAILED", report["oracle_naive_subset"])
    return 0 if oracle_ok and final_status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
