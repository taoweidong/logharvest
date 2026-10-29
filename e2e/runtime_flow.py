"""LogHarvest 运行时流程：作业取消状态机、源文件漂移、只读分析聚合对账。

这个流程只补 api_flow / rbac_flow 明确留白的三块，不重复它们已经钉死的东西：

- **取消（R1）**：``POST /jobs/{id}/cancel`` 的三种前置态（排队中 / 运行中 / 已终态），
  ``CLAIM_SQL`` 的 ``j.cancel_requested = false`` 守卫，取消与重试的互斥，以及
  ``job.cancel`` / ``job.retry_failed`` 两条审计证据——rbac_flow 里这两条是 SKIP，
  因为不想在权限矩阵里跑真实作业，证据归这里。
- **源文件漂移（R2）**：作业创建后 NAS 上的归档被改写，执行期 ``_check_source`` 必须
  判永久失败（不烧 max_attempts、不发布半截命中），人工恢复后 ``retry-failed`` 才跑通。
  这条必须在真实 NAS 上做：mock 出来的"文件大小不符"证明不了 virtiofs 的 stat 语义。
- **只读聚合（R3）**：``/api/stats/jobs`` 与 ``/api/rules``。这两个接口是概览页和数据
  看板的数据源，口径错在页面上只表现为"数字看着还行"，所以全部用独立重算对账。

NAS 的 ``/nas/cancel``（约 60MB 存储型 zip）是特意选的：单文件、跑得够久，才能在真实的
running 窗口里取消；``/nas/modified`` 只在排空前独占改写，避免运行中的 Worker 扫到正在
被重写的目录。

运行：
    PYTHONIOENCODING=utf-8 service/.venv/Scripts/python.exe e2e/runtime_flow.py
    PYTHONIOENCODING=utf-8 service/.venv/Scripts/python.exe e2e/runtime_flow.py --phase cancel

退出码 0 = 全部断言通过；1 = 有 FAIL（明细见 e2e/artifacts/ledger_runtime_flow.json）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from client import (  # noqa: E402
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    API_BASE,
    TERMINAL_JOB_STATUSES,
    ApiClient,
    ApiError,
    Ledger,
    load_fixtures,
    load_ruleset_payload,
)

# 复用 api_flow 的公共工具：断言口径与翻页实现必须和主流程一致，不要在这里重写一份。
from api_flow import (  # noqa: E402
    create_job,
    expect_error,
    iter_matches,
    list_all,
    open_sse,
)

E2E_DIR = Path(__file__).resolve().parent
RUN_ID = str(time.time_ns())

# 契约 §6.1 / §6.2 的响应字段全集（写死才有意义：字段增减是接口契约变更，这里必须失败）。
JOB_FIELDS = [
    "cancel_requested", "created_at", "created_by", "data_source_id", "error",
    "finished_at", "id", "name", "rule_set_id", "source_path", "started_at",
    "status", "total_bytes", "total_files",
]
TASK_FIELDS = [
    "attempts", "error", "file_name", "heartbeat_at", "id", "job_id", "matched_rows",
    "max_attempts", "processed_lines", "scheduled_at", "size_bytes", "source_path",
    "status", "worker_id",
]
RULE_FIELDS = [
    "rule_set_id", "rule_set_name", "version", "rule_id", "type", "severity", "summary",
]
STATS_FIELDS = ["days", "data_source_id", "totals", "daily", "top_rules"]
TOTALS_FIELDS = [
    "bytes_total", "cancelled", "failed", "file_failed", "file_succeeded", "files_total",
    "jobs", "matched_rows_total", "partial", "running", "succeeded",
]


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_ts(value: Any) -> datetime:
    """API 时间戳统一是 UTC ISO（可能带 Z）；naive 一律当 UTC，否则窗口会整体偏移时区。"""
    text = str(value)
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def zulu(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def wait_until(what: Callable[[], Any], *, timeout: float, name: str, interval: float = 0.5) -> Any:
    """等到条件为真并返回它；超时抛 TimeoutError，绝不返回 None 冒充成功。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = what()
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError(f"{name} 未在 {timeout}s 内成立")


def tasks_of(api: ApiClient, job_id: int) -> list[dict[str, Any]]:
    return list_all(api, f"/jobs/{job_id}/files")


def counters(tasks: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for task in tasks:
        out[task["status"]] = out.get(task["status"], 0) + 1
    return out


def matches_of(api: ApiClient, job_id: int) -> list[dict[str, Any]]:
    """按作业 scope 拉命中；R1/R2 的作业都是小作业，翻页成本可接受。"""
    return list(iter_matches(api, page=500, job_id=job_id))


def audit_rows(api: ApiClient, action: str, *, resource_id: Any = None) -> list[dict[str, Any]]:
    """审计查询走服务端过滤（action / resource_id 都是 /operation-logs 的真实参数）。"""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        query = f"limit=500&offset={offset}&action={action}"
        if resource_id is not None:
            query += f"&resource_id={resource_id}"
        page = api.get(f"/operation-logs?{query}")
        rows.extend(page)
        if len(page) < 500:
            return rows
        offset += 500


def workers_of(api: ApiClient) -> list[dict[str, Any]]:
    """/workers 的 limit 上限是 200（api_flow 已钉过 201→422），不能用 list_all 的默认 500。"""
    return list_all(api, "/workers", page=200)


# ============================================================================
# 前置：Worker 闸门与身份
# ============================================================================
def drain_all(ledger: Ledger, api: ApiClient) -> list[dict[str, Any]]:
    """排空全部 Worker：让任务停在 pending，用来制造"排队中被取消"与"改写时无人扫"。

    ``drain_requested`` 由 API 置位，``status`` 要等 Worker 下一轮心跳自报
    （up.sh 把轮询压到 1s），所以两个字段分开断言——只看 API 侧置位会把
    "Worker 根本没消费这个标记"这类缺陷放过去。
    """
    workers = workers_of(api)
    ledger.check("排空前有可用 Worker", len(workers) >= 2, f"workers={[w['id'] for w in workers]}")
    for worker in workers:
        body = api.post(f"/workers/{worker['id']}/drain", expect_status=200)
        ledger.equal(
            f"drain worker {worker['id']} 返回形状",
            sorted(body),
            sorted(["id", "drain_requested", "status"]),
        )
    rows = wait_until(
        lambda: [w for w in workers_of(api) if w["status"] == "draining"],
        timeout=45.0,
        name="全部 Worker 自报 draining",
        interval=1.0,
    )
    ledger.check(
        f"{len(rows)} 个 Worker 全部 draining",
        len(rows) == len(workers),
        f"draining={[r['id'] for r in rows]} all={[w['id'] for w in workers]}",
    )
    return workers


def resume_all(ledger: Ledger, api: ApiClient) -> None:
    """恢复认领，并确认标记真的清零——遗留的 drain 标记会让后面的作业永远 pending。"""
    for worker in workers_of(api):
        body = api.post(f"/workers/{worker['id']}/resume", expect_status=200)
        ledger.equal(f"resume worker {worker['id']} 复位", (body["drain_requested"], body["status"]), (False, "online"))
    wait_until(
        lambda: not any(w["drain_requested"] for w in workers_of(api)),
        timeout=30.0,
        name="全部 Worker 的 drain 标记清零",
        interval=1.0,
    )
    ledger.check("恢复后无 drain 标记", True, "drain_requested 全为 False")


def bootstrap(ledger: Ledger, api: ApiClient) -> dict[str, Any]:
    ledger.phase("R0 前置：登录、复用 e2e 数据源、提交本轮规则集")
    fixtures = load_fixtures()["cases"]
    token = api.login(ADMIN_USERNAME, ADMIN_PASSWORD)
    ledger.check("管理员登录拿到 token", bool(token), f"token_len={len(token)}")

    # 数据源复用 api_flow 建的 e2e-nas；重跑时不再创建，避免与主流程互相污染。
    sources = api.get("/data-sources", expect_status=200)
    source = next((s for s in sources if s["name"] == "e2e-nas"), None)
    if source is None:
        source = api.post(
            "/data-sources",
            {
                "name": "e2e-nas",
                "root_path": "/nas",
                "read_only": True,
                "copy_to_local": True,
                "description": "E2E 只读 NAS（runtime_flow 复用）",
            },
            expect_status=201,
        )
        ledger.check("数据源 e2e-nas 不存在时新建", True, f"id={source['id']}")
    else:
        ledger.check("复用 api_flow 建好的数据源 e2e-nas", True, f"id={source['id']}")
    ledger.equal("数据源只读（漂移用例前提）", source["read_only"], True)

    # 规则集每次提交自增新版本：作业绑定具体版本，历史作业不会被后续提交改写。
    config = load_ruleset_payload("canonical_ruleset.json")
    created = api.post("/rule-sets", {"name": config["name"], "config": config}, expect_status=201)
    ctx = {
        "ds_id": source["id"],
        "rule_set_id": created["id"],
        "rule_set_version": created["version"],
        "fixtures": fixtures,
        "rule_ids": [rule["id"] for rule in config["rules"]],
    }
    ledger.check("规则集版本自增（本轮新建）", created["version"] >= 1, str(created["version"]))
    ledger.check("取消/漂移夹具齐备", {"cancel", "modified"} <= set(fixtures), sorted(fixtures))
    ledger.check(
        "cancel 夹具是单文件大归档（要够久才能跑到运行中取消）",
        fixtures["cancel"]["expect_total_files"] == 1 and fixtures["cancel"].get("approx_bytes", 0) > 20_000_000,
        json.dumps({k: fixtures["cancel"].get(k) for k in ("expect_total_files", "approx_bytes", "archive")},
                   ensure_ascii=False),
    )
    return ctx


# ============================================================================
# R1 作业取消状态机
# ============================================================================
def phase_cancel(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("R1 作业取消状态机（排队取消 / 运行中取消 / 终态空操作）")
    spec = ctx["fixtures"]["cancel"]
    workers = drain_all(ledger, api)

    # --- A：任务还全是 pending 时取消，作业必须立刻终态，不留悬挂 ---
    job_a = create_job(
        api,
        name=f"e2e-cancel-queued-{RUN_ID}",
        data_source_id=ctx["ds_id"],
        source_path=spec["source"],
        rule_set_id=ctx["rule_set_id"],
        idem=f"e2e-rt-queued-{RUN_ID}",
    )
    ledger.equal("A 创建响应字段集 = JobRead", sorted(job_a), sorted(JOB_FIELDS))
    ledger.equal("A 有文件的作业创建即 running", job_a["status"], "running")
    ledger.equal("A total_files 取夹具 oracle", job_a["total_files"], spec["expect_total_files"])
    ledger.check("A total_bytes 等于 NAS 上归档字节数", job_a["total_bytes"] > 20_000_000, str(job_a["total_bytes"]))
    ledger.check("A 建好时 finished_at 为空", job_a["finished_at"] is None, str(job_a["finished_at"]))
    ledger.equal("A 建好时 cancel_requested 为 false", job_a["cancel_requested"], False)

    pending = tasks_of(api, job_a["id"])
    ledger.equal("A 文件任务行数", len(pending), spec["expect_total_files"])
    ledger.equal("A 排空前任务字段集 = FileTaskRead", sorted(pending[0]), sorted(TASK_FIELDS))
    ledger.equal("A 排空后任务确实停在 pending", counters(pending), {"pending": len(pending)})
    ledger.check(
        "A 规划期快照 size/mtime 已落库",
        all(t["size_bytes"] > 0 and bool(t["scheduled_at"]) and t["worker_id"] is None for t in pending),
        json.dumps([{k: t[k] for k in ("size_bytes", "worker_id", "attempts")} for t in pending], ensure_ascii=False),
    )

    cancelled_a = api.post(f"/jobs/{job_a['id']}/cancel", expect_status=200)
    ledger.equal("A 取消响应字段集仍是 JobRead", sorted(cancelled_a), sorted(JOB_FIELDS))
    ledger.equal("A 无 running 任务 → 直接 cancelled", cancelled_a["status"], "cancelled")
    ledger.equal("A cancel_requested 置位", cancelled_a["cancel_requested"], True)
    ledger.check("A 终态写了 finished_at", bool(cancelled_a["finished_at"]), str(cancelled_a["finished_at"]))
    ledger.equal("A 取消不改写 total_bytes", cancelled_a["total_bytes"], job_a["total_bytes"])
    ledger.equal("A 取消不写 job.error（取消不是失败）", cancelled_a["error"], None)

    after_a = tasks_of(api, job_a["id"])
    ledger.equal("A pending 任务整批翻 cancelled", counters(after_a), {"cancelled": len(after_a)})
    ledger.check(
        "A 被取消的任务没有 worker/心跳/进度（从未被认领）",
        all(t["worker_id"] is None and t["heartbeat_at"] is None for t in after_a),
        json.dumps([{k: t[k] for k in ("worker_id", "heartbeat_at")} for t in after_a], ensure_ascii=False),
    )
    ledger.check(
        "A 取消任务的进度计数为 0/None 且 error 为空",
        all(not t["processed_lines"] and not t["matched_rows"] and t["error"] is None for t in after_a),
        json.dumps([{k: t[k] for k in ("processed_lines", "matched_rows", "error")} for t in after_a], ensure_ascii=False),
    )
    ledger.equal("A 取消后没有发布命中", len(matches_of(api, job_a["id"])), 0)

    # CLAIM_SQL 的 j.cancel_requested=false 是这条守卫的全部意义：置位后若还能被领走，
    # 已取消的作业会继续吃 CPU 并可能把任务改回 running，终态就不可信了。
    time.sleep(6.0)  # > 5 个轮询周期（LOGHARVEST_WORKER_POLL_SECONDS=1.0）
    later_a = counters(tasks_of(api, job_a["id"]))
    ledger.equal("A 6s 后任务没被重新认领", later_a, {"cancelled": len(after_a)})
    ledger.equal("A 6s 后作业仍是 cancelled", api.get(f"/jobs/{job_a['id']}")["status"], "cancelled")

    # --- 取消与重试互斥、幂等与负面用例 ---
    retry_a = api.call("POST", f"/jobs/{job_a['id']}/files/retry-failed")
    ledger.equal("A 已取消作业重试被拒（HTTP）", retry_a["status"], 409)
    ledger.equal("A 已取消作业重试被拒（逐字文案）", retry_a["body"].get("detail"), "cancelled jobs cannot be retried")
    ledger.equal("A 被拒的重试没有改状态", api.get(f"/jobs/{job_a['id']}")["status"], "cancelled")

    again_a = api.post(f"/jobs/{job_a['id']}/cancel", expect_status=200)
    ledger.equal("A 重复取消状态不变", again_a["status"], "cancelled")
    ledger.equal("A 重复取消 finished_at 未被刷新", again_a["finished_at"], cancelled_a["finished_at"])

    expect_error(ledger, "取消未知作业 → 404", lambda: api.post("/jobs/999999/cancel"), 404, contains="job not found")
    unknown_retry = api.call("POST", "/jobs/999999/files/retry-failed")
    ledger.equal("重试未知作业 → 404", (unknown_retry["status"], unknown_retry["body"].get("detail")), (404, "job not found"))

    rows_a = audit_rows(api, "job.cancel", resource_id=job_a["id"])
    ledger.check("审计有本轮的 job.cancel 证据", len(rows_a) >= 2, f"rows={len(rows_a)}")
    ledger.equal("job.cancel 记录的是取消后的终态", rows_a[0]["detail"], {"after": {"status": "cancelled", "cancel_requested": True}})
    ledger.check("job.cancel 审计带 actor/resource 快照",
                 rows_a[0]["actor_name"] == ADMIN_USERNAME and rows_a[0]["resource_type"] == "job"
                 and str(rows_a[0]["resource_id"]) == str(job_a["id"]),
                 json.dumps({k: rows_a[0][k] for k in ("actor_name", "resource_type", "resource_id", "summary")},
                            ensure_ascii=False))
    ledger.check("审计摘要不含口令类敏感串",
                 all("password" not in json.dumps(r, ensure_ascii=False).lower() for r in rows_a), str(len(rows_a)))

    # --- SSE 与取消：已终态的作业读流必须立刻拿到 cancelled 帧并自然结束 ---
    _, frames_a = open_sse(api, f"/jobs/{job_a['id']}/events?max_events=5")
    ledger.check("A 已终态作业的 SSE 至少一帧", len(frames_a) >= 1, f"frames={len(frames_a)}")
    ledger.equal("A SSE 首帧 status=cancelled", frames_a[0]["status"], "cancelled")
    ledger.equal("A SSE 首帧 by_status 与任务表一致",
                 frames_a[0]["by_status"], {"cancelled": len(after_a)})
    ledger.equal("A SSE 首帧 total 等于任务数", frames_a[0]["total"], len(after_a))

    # --- B：运行中取消。60MB 归档给了真实的 running 窗口，SSE 用线程并行采集 ---
    resume_all(ledger, api)
    job_b = create_job(
        api,
        name=f"e2e-cancel-inflight-{RUN_ID}",
        data_source_id=ctx["ds_id"],
        source_path=spec["source"],
        rule_set_id=ctx["rule_set_id"],
        idem=f"e2e-rt-inflight-{RUN_ID}",
    )
    sse: dict[str, Any] = {}

    def collect() -> None:
        try:
            sse["headers"], sse["frames"] = open_sse(api, f"/jobs/{job_b['id']}/events?max_events=100")
        except BaseException as exc:  # noqa: BLE001
            sse["error"] = f"{type(exc).__name__}: {exc}"

    collector = threading.Thread(target=collect, daemon=True)
    collector.start()
    try:
        running = wait_until(
            lambda: [t for t in tasks_of(api, job_b["id"]) if t["status"] == "running"],
            timeout=120.0,
            name="B 出现 running 任务",
            interval=0.25,
        )
        inflight = api.post(f"/jobs/{job_b['id']}/cancel", expect_status=200)
        ledger.equal("B cancel_requested 置位", inflight["cancel_requested"], True)
        ledger.equal("B 有 running 任务时作业不得提前终态", inflight["status"], "running")
        ledger.check("B running 中 finished_at 仍为空", inflight["finished_at"] is None, str(inflight["finished_at"]))
        ledger.equal("B 取消返回的是同一作业", inflight["id"], job_b["id"])

        job_b_final = api.wait_job_terminal(job_b["id"], timeout=600.0, interval=1.0)
        ledger.equal("B 终态 cancelled", job_b_final["status"], "cancelled")
        ledger.check("B 终态补写 finished_at", bool(job_b_final["finished_at"]), str(job_b_final["finished_at"]))
        tasks_b = tasks_of(api, job_b["id"])
        status_b = {t["status"] for t in tasks_b}
        ledger.check(
            "B 任务终态落在 {cancelled, succeeded} 内",
            status_b <= {"cancelled", "succeeded"},
            json.dumps(counters(tasks_b), ensure_ascii=False),
        )
        task_b = tasks_b[0]
        # 取消是协作式的：处理器只在心跳预算里查标志（每 2 万行或最迟 5 秒一次），
        # 所以"任务恰好在这一小段跑完"会 legitimate 地留下 succeeded。把两种结果都记下来。
        if task_b["status"] == "cancelled":
            ledger.equal("B 协作取消把任务判 cancelled", task_b["status"], "cancelled")
            # 取消原因 "job cancelled" 只存在于 Worker 侧的 TaskResult/日志：repo.complete 的
            # cancelled 分支不传 error，任务行的 error 恒为 NULL（与 A 的排队取消同口径——
            # 取消不是失败）。运维要区分"跑了一半被取消"和"没跑就被取消"看 processed_lines。
            ledger.equal("B 取消任务的 error 不落库（取消不是失败）", task_b["error"], None)
            ledger.check(
                "B 取消任务保留了已扫描进度（不谎报为 0）",
                (task_b["processed_lines"] or 0) > 0,
                f"processed_lines={task_b['processed_lines']} matched_rows={task_b['matched_rows']}",
            )
            published_b = len(matches_of(api, job_b["id"]))
            counter_b = task_b["matched_rows"] or 0
            ledger.check(
                "B 已发布命中数 ≤ 任务计数（取消时未落盘的缓冲丢失，不反号）",
                published_b <= counter_b,
                f"published={published_b} counter={counter_b}",
            )
        else:
            ledger.skip(
                "B 任务在取消标志生效前跑完（协作式取消的心跳窗口）",
                f"status={task_b['status']} processed_lines={task_b['processed_lines']} matched_rows={task_b['matched_rows']}",
            )
        ledger.equal("B 至少有一次真实认领（worker_id 非空）", bool(task_b["worker_id"]), True)
        ledger.check("B attempts 恰为 1（取消不触发重试）", task_b["attempts"] == 1, str(task_b["attempts"]))
    finally:
        collector.join(timeout=200.0)

    ledger.check("B 的 SSE 流读完了", "frames" in sse, sse.get("error", "still running"))
    if "frames" in sse:
        frames_b = sse["frames"]
        ledger.check("B 的 SSE 采到多帧进度", len(frames_b) >= 2, f"frames={len(frames_b)}")
        ledger.equal("B 的 SSE 首帧 status=running", frames_b[0]["status"], "running")
        ledger.equal("B 的 SSE 末帧 status=cancelled", frames_b[-1]["status"], "cancelled")
        ledger.equal("B 的 SSE 末帧 job_id", frames_b[-1]["job_id"], job_b["id"])
        ledger.check(
            "B 的 SSE 帧序里 status 只能由 running 转终态",
            {f["status"] for f in frames_b} <= {"running", "cancelled"},
            str(sorted({f["status"] for f in frames_b})),
        )
        ledger.check(
            "B 的 SSE 进度单调不回退",
            all(a["total"] == b["total"] for a, b in zip(frames_b, frames_b[1:])),
            str([f["total"] for f in frames_b][:8]),
        )
    rows_b = audit_rows(api, "job.cancel", resource_id=job_b["id"])
    ledger.equal("B 运行中取消只记一条审计", len(rows_b), 1)
    ledger.equal("B 审计 after 快照（running + 已置位）",
                 rows_b[0]["detail"], {"after": {"status": "running", "cancel_requested": True}})
    retry_b = api.call("POST", f"/jobs/{job_b['id']}/files/retry-failed")
    ledger.equal("B 运行中取消的作业同样拒绝重试", (retry_b["status"], retry_b["body"].get("detail")),
                 (409, "cancelled jobs cannot be retried"))

    # --- C：已成功的作业取消是纯空操作，且不能顺手把 cancel_requested 打开 ---
    clean_spec = ctx["fixtures"]["clean"]
    job_c = create_job(
        api,
        name=f"e2e-cancel-terminal-{RUN_ID}",
        data_source_id=ctx["ds_id"],
        source_path=clean_spec["source"],
        rule_set_id=ctx["rule_set_id"],
        idem=f"e2e-rt-terminal-{RUN_ID}",
    )
    job_c_final = api.wait_job_terminal(job_c["id"], timeout=300.0)
    ledger.equal("C clean 作业先跑成功", job_c_final["status"], "succeeded")
    ledger.equal("C 成功作业的命中数与夹具一致",
                 sum(t["matched_rows"] or 0 for t in tasks_of(api, job_c["id"])),
                 clean_spec["expect_total_matches"])
    canceled_c = api.post(f"/jobs/{job_c['id']}/cancel", expect_status=200)
    ledger.equal("C 终态作业取消是空操作（状态）", canceled_c["status"], "succeeded")
    ledger.equal("C 终态作业取消不改 cancel_requested", canceled_c["cancel_requested"], False)
    ledger.equal("C 终态作业取消不刷新 finished_at", canceled_c["finished_at"], job_c_final["finished_at"])
    # 一次调用只留一条 job.retry_failed 审计，所以下面 C 的审计行数才是可断言的常量。
    retry_c = api.call("POST", f"/jobs/{job_c['id']}/files/retry-failed")
    ledger.equal("C 成功作业重试失败任务 → 200", retry_c["status"], 200)
    ledger.equal("C 无失败任务时重试计数为 0", retry_c["body"], {"retried": 0, "remaining_failed": 0})
    ledger.equal("C 空重试不把终态作业拉回 running", api.get(f"/jobs/{job_c['id']}")["status"], "succeeded")
    rows_cr = audit_rows(api, "job.retry_failed", resource_id=job_c["id"])
    ledger.equal("C 重试审计只落一条", len(rows_cr), 1)
    ledger.equal("C 重试审计 detail.counts", rows_cr[0]["detail"], {"counts": {"retried": 0, "remaining_failed": 0}})
    rows_c = audit_rows(api, "job.cancel", resource_id=job_c["id"])
    ledger.equal("C 空操作取消也留痕（状态未变）",
                 rows_c[0]["detail"], {"after": {"status": "succeeded", "cancel_requested": False}})

    drain_all(ledger, api)
    ledger.check("R1 收尾把 Worker 留在排空态（交给 R2 独占改写 NAS）", True, f"workers={len(workers)}")


# ============================================================================
# R2 源文件漂移（规划快照 vs 执行期 stat）
# ============================================================================
NAS_HOST_DIR = E2E_DIR / "nas"
DRIFT_BYTES = 64


def phase_drift(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("R2 源文件漂移（改写 → 永久失败 → 恢复 → 人工重试跑通）")
    spec = ctx["fixtures"]["modified"]
    target = NAS_HOST_DIR / "modified" / "mod.zip"
    ledger.check("漂移目标归档在宿主 NAS 目录上存在", target.is_file(), str(target))

    # 单独跑 --phase drift 时也必须先占住 NAS：运行中的 Worker 不该扫到正在被重写的目录。
    drain_all(ledger, api)
    original = target.read_bytes()
    stamp = target.stat()
    orig_size, orig_mtime_ns = stamp.st_size, stamp.st_mtime_ns
    ledger.check("mod.zip 非空且 size/mtime 可作为快照基线", orig_size > 0, f"size={orig_size} mtime_ns={orig_mtime_ns}")

    job = create_job(
        api,
        name=f"e2e-drift-{RUN_ID}",
        data_source_id=ctx["ds_id"],
        source_path=spec["source"],
        rule_set_id=ctx["rule_set_id"],
        idem=f"e2e-rt-drift-{RUN_ID}",
    )
    tasks = tasks_of(api, job["id"])
    ledger.equal("漂移作业的任务行数 = 夹具 oracle", len(tasks), spec["expect_total_files"])
    planned = tasks[0]
    ledger.equal("规划期把 NAS 上的字节数快照进 size_bytes", planned["size_bytes"], orig_size)
    ledger.check(
        "排空期间任务未被认领（worker/心跳为空）",
        planned["worker_id"] is None and planned["heartbeat_at"] is None,
        json.dumps({k: planned[k] for k in ("worker_id", "heartbeat_at", "status")}, ensure_ascii=False),
    )

    # 只改 size 不改 mtime 以外的东西：追加 64 字节后，_check_source 的第一道校验必然先命中。
    target.write_bytes(original + b" " * DRIFT_BYTES)
    ledger.equal("宿主侧改写后的字节数", target.stat().st_size, orig_size + DRIFT_BYTES)
    error_expected = f"source size changed: expected {orig_size}, found {orig_size + DRIFT_BYTES}"

    resume_all(ledger, api)
    try:
        drifted = api.wait_job_terminal(job["id"], timeout=300.0, interval=1.0)
        ledger.equal("源被改写 → 作业 failed", drifted["status"], "failed")
        # 失败原因只落在任务上，job.error 由产品契约保持为空：概览与列表都不编造作业级错误。
        ledger.equal("作业级 error 保持为空", drifted["error"], None)
        ledger.equal("漂移不置 cancel_requested", drifted["cancel_requested"], False)
        ledger.check("failed 作业写了 finished_at", bool(drifted["finished_at"]), str(drifted["finished_at"]))

        task_d = tasks_of(api, job["id"])[0]
        ledger.equal("漂移任务 status=failed", task_d["status"], "failed")
        # 文案里的 found 是 Worker 容器内 os.stat 读到的数字：它等于宿主 size，就同时证明了
        # virtiofs 的 size 传播与"执行期重新 stat"这两件事，而不是 API 拿规划数据自说自话。
        ledger.equal("漂移错误文案逐字命中，且容器看到改写后的字节数", task_d["error"], error_expected)
        ledger.equal("永久失败不烧 max_attempts：attempts 恰为 1", task_d["attempts"], 1)
        ledger.check("永久失败没有半截进度", not task_d["processed_lines"] and not task_d["matched_rows"],
                     f"processed={task_d['processed_lines']} matched={task_d['matched_rows']}")
        ledger.equal("漂移作业没有发布任何命中", len(matches_of(api, job["id"])), 0)

        # 未恢复时重试：允许（不是 cancelled 作业），但必然同样失败，且尝试历史要累加而不是重置。
        retry_drifted = api.call("POST", f"/jobs/{job['id']}/files/retry-failed")
        ledger.equal("失败作业允许人工重试 → 200", retry_drifted["status"], 200)
        ledger.equal("重试入队计数", retry_drifted["body"], {"retried": 1, "remaining_failed": 0})
        requeued = api.get(f"/jobs/{job['id']}")
        ledger.equal("有任务回到队列 → 作业从终态拉回 running", requeued["status"], "running")
        ledger.check("拉回 running 时 finished_at 清空", requeued["finished_at"] is None, str(requeued["finished_at"]))
        again = api.wait_job_terminal(job["id"], timeout=300.0, interval=1.0)
        task_again = tasks_of(api, job["id"])[0]
        ledger.equal("源仍未恢复 → 重试再次 failed", again["status"], "failed")
        ledger.equal("手工重试保留尝试历史（attempts 累加到 2）", task_again["attempts"], 2)
        ledger.equal("第二次失败原因与第一次逐字相同", task_again["error"], error_expected)
    finally:
        # 无论断言/超时走到哪一步，NAS 都必须还原成别的流程读到的样子（字节 + ns 级 mtime）。
        target.write_bytes(original)
        os.utime(target, ns=(stamp.st_atime_ns, orig_mtime_ns))

    restored = target.stat()
    ledger.equal("还原后宿主字节数回到基线", restored.st_size, orig_size)
    # _check_source 的第二道校验按整秒比 mtime；ns 级还原是 virtiofs 往返保真的实测结论。
    ledger.check("还原后 mtime 精确到 ns 回到基线", restored.st_mtime_ns == orig_mtime_ns,
                 f"now={restored.st_mtime_ns} base={orig_mtime_ns}")

    retry_restored = api.call("POST", f"/jobs/{job['id']}/files/retry-failed")
    ledger.equal("恢复后重试 → 200 且入队 1 个任务", retry_restored["body"], {"retried": 1, "remaining_failed": 0})
    recovered = api.wait_job_terminal(job["id"], timeout=300.0, interval=1.0)
    ledger.equal("人工恢复后作业跑通 → succeeded", recovered["status"], "succeeded")
    task_ok = tasks_of(api, job["id"])[0]
    ledger.equal("成功任务的 attempts 记到 3（历史不被重置）", task_ok["attempts"], 3)
    ledger.equal("重试把任务级 error 清空", task_ok["error"], None)
    ledger.equal("恢复后扫描行数 = 夹具 oracle", task_ok["processed_lines"], spec["expect_total_lines"])
    ledger.equal("恢复后命中计数 = 夹具 oracle", task_ok["matched_rows"], spec["expect_total_matches"])
    published = matches_of(api, job["id"])
    # 成功路径与取消路径的分工：这里没有缓冲丢失，命中表必须数出与计数器完全相同的行数。
    ledger.equal("成功任务的命中计数与命中表完全一致", len(published), task_ok["matched_rows"])
    by_rule: dict[str, int] = {}
    for row in published:
        by_rule[row["rule_id"]] = by_rule.get(row["rule_id"], 0) + 1
    ledger.equal("命中按规则分布 = 夹具 oracle", by_rule, spec["expect_matches_by_rule"])
    ledger.check("命中的规则 id 全部来自本轮规则集", set(by_rule) <= set(ctx["rule_ids"]),
                 f"hit_rules={sorted(by_rule)} run_rules={ctx['rule_ids']}")
    ledger.check("恢复后 worker_id 归属仍然可追溯", bool(task_ok["worker_id"]), str(task_ok["worker_id"]))

    rows = audit_rows(api, "job.retry_failed", resource_id=job["id"])
    ledger.equal("本作业的人工重试审计共 2 条", len(rows), 2)
    ledger.equal("最新一条审计带 counts 明细", rows[0]["detail"], {"counts": {"retried": 1, "remaining_failed": 0}})
    ledger.check("重试审计不含口令类敏感串",
                 all("password" not in json.dumps(r, ensure_ascii=False).lower() for r in rows), str(len(rows)))
    ledger.check("R2 收尾把 NAS 还原、Worker 留在可认领态",
                 target.stat().st_size == orig_size, f"workers={[w['status'] for w in workers_of(api)]}")


# ============================================================================
# R3 只读分析聚合对账
# ============================================================================
DAILY_FIELDS = ["bytes_total", "date", "files_failed", "files_succeeded", "jobs_created", "matched_rows"]
TOP_RULE_FIELDS = ["hits", "jobs", "rule_id", "rule_version"]
# 窗口边界安全带：距 cutoff 这么近的作业会让"服务端算的窗口"和"客户端算的窗口"因为
# 几次毫秒的时钟差而落到不同集合上。带内没有作业时，两份集合才是同一个，重算才有意义。
EDGE_MARGIN_SECONDS = 600
JOB_STATUSES = {"running", "succeeded", "partial", "failed", "cancelled"}


def _in_window(jobs: list[dict[str, Any]], days: int) -> tuple[datetime, list[dict[str, Any]], list[dict[str, Any]]]:
    cutoff = utcnow() - timedelta(days=days)
    inside = [job for job in jobs if parse_ts(job["created_at"]) >= cutoff]
    near_edge = [
        job for job in jobs
        if abs((parse_ts(job["created_at"]) - cutoff).total_seconds()) <= EDGE_MARGIN_SECONDS
    ]
    return cutoff, inside, near_edge


def _recompute_stats(
    jobs: list[dict[str, Any]],
    tasks_by_job: dict[int, list[dict[str, Any]]],
    matches: list[dict[str, Any]],
    *,
    days: int,
    data_source_id: int | None,
) -> dict[str, Any]:
    """纯 Python 复算 §8 口径：窗口只按 jobs.created_at，文件与命中一律经其所属作业计入。

    刻意不复用服务端的任何 SQL——字节数取 ``Σ file_tasks.size_bytes``（不是
    ``jobs.total_bytes``），文件数取任务行数（不是 ``jobs.total_files``），命中数取
    ``matches`` 表行数（不是任务计数器）。这三处正是"看着像但口径不同"的地方。
    """
    _, inside, near_edge = _in_window(jobs, days)
    if data_source_id is not None:
        inside = [job for job in inside if job["data_source_id"] == data_source_id]
    day_of = {job["id"]: parse_ts(job["created_at"]).date().isoformat() for job in inside}
    ids = set(day_of)
    status_counts: dict[str, int] = {}
    for job in inside:
        status_counts[job["status"]] = status_counts.get(job["status"], 0) + 1

    tasks = [task for job in inside for task in tasks_by_job.get(job["id"], [])]
    in_window = [match for match in matches if match["job_id"] in ids]
    totals = {
        "jobs": len(inside),
        "succeeded": status_counts.get("succeeded", 0),
        "partial": status_counts.get("partial", 0),
        "failed": status_counts.get("failed", 0),
        "cancelled": status_counts.get("cancelled", 0),
        "running": status_counts.get("running", 0),
        "files_total": len(tasks),
        "bytes_total": sum(int(task["size_bytes"] or 0) for task in tasks),
        "matched_rows_total": len(in_window),
        "file_succeeded": sum(1 for task in tasks if task["status"] == "succeeded"),
        "file_failed": sum(1 for task in tasks if task["status"] == "failed"),
    }

    buckets: dict[str, dict[str, int]] = {}

    def bucket(day: str) -> dict[str, int]:
        return buckets.setdefault(day, {
            "jobs_created": 0, "files_succeeded": 0, "files_failed": 0,
            "matched_rows": 0, "bytes_total": 0,
        })

    for job in inside:
        bucket(day_of[job["id"]])["jobs_created"] += 1
    for task in tasks:
        row = bucket(day_of[task["job_id"]])
        row["bytes_total"] += int(task["size_bytes"] or 0)
        if task["status"] == "succeeded":
            row["files_succeeded"] += 1
        elif task["status"] == "failed":
            row["files_failed"] += 1
    for match in in_window:
        bucket(day_of[match["job_id"]])["matched_rows"] += 1

    daily = [{"date": day, **buckets[day]} for day in sorted(buckets)]

    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    for match in in_window:
        entry = grouped.setdefault((match["rule_id"], int(match["rule_version"])),
                                   {"rule_id": match["rule_id"], "rule_version": int(match["rule_version"]),
                                    "hits": 0, "_jobs": set()})
        entry["hits"] += 1
        entry["_jobs"].add(match["job_id"])
    top_rules = [
        {"rule_id": e["rule_id"], "rule_version": e["rule_version"], "hits": e["hits"], "jobs": len(e["_jobs"])}
        for e in grouped.values()
    ]
    # 与 _top_rules 同序：命中数降序，再按 (rule_id, rule_version) 升序稳定，取前 10。
    top_rules.sort(key=lambda row: (-row["hits"], row["rule_id"], row["rule_version"]))
    return {
        "totals": totals,
        "daily": daily,
        "top_rules": top_rules[:10],
        "top_rules_all": top_rules,
        "near_edge": near_edge,
        "inside_ids": ids,
    }


def phase_stats(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("R3 只读分析：/stats/jobs 独立重算 + /rules 展平索引")

    for name, query in [
        ("days=0 越下界", "stats/jobs?days=0"),
        ("days=91 越上界", "stats/jobs?days=91"),
        ("days=abc 非整数", "stats/jobs?days=abc"),
        ("data_source_id=0 越下界", "stats/jobs?data_source_id=0"),
    ]:
        expect_error(ledger, f"参数校验 {name} → 422", lambda q=query: api.get(q), 422)
    wrong_shape = api.call("GET", "/stats/jobs?days=0")
    ledger.check("框架 422 的 detail 是数组（不是业务字符串）", isinstance(wrong_shape["body"].get("detail"), list),
                 json.dumps(wrong_shape["body"], ensure_ascii=False)[:200])

    # 快照：作业、每个作业的任务、窗口内全部命中。命中按"自己的" created_at 多取 1 小时，
    # 保证窗口边缘作业（作业时间在前、写命中在后）的行一条不落，之后一律按作业集合再过滤。
    jobs_a = list_all(api, "/jobs")
    ledger.check("作业列表非空（前面的流程已产生数据）", len(jobs_a) >= 5, f"jobs={len(jobs_a)}")
    statuses = {job["status"] for job in jobs_a}
    ledger.check("作业状态全集落在契约枚举内", statuses <= JOB_STATUSES, str(sorted(statuses)))

    horizon = utcnow() - timedelta(days=14, hours=1)
    matches_snapshot = list(iter_matches(
        api, page=500,
        created_from=zulu(horizon),
        created_to=zulu(utcnow() + timedelta(hours=1)),
    ))
    ledger.check("命中快照非空", len(matches_snapshot) > 0, f"matches={len(matches_snapshot)}")

    tasks_by_job: dict[int, list[dict[str, Any]]] = {}
    for job in jobs_a:
        tasks_by_job[job["id"]] = tasks_of(api, job["id"])
    jobs_b = list_all(api, "/jobs")
    # 重算的前提是"数据没在脚下动"：采样期间没有新作业、没有状态迁移，两次快照必须逐字段相同。
    ledger.equal("作业快照稳定（无并发写入/状态迁移）",
                 [(j["id"], j["status"], j["created_at"]) for j in jobs_b],
                 [(j["id"], j["status"], j["created_at"]) for j in jobs_a])

    for days in (14, 1):
        stats = api.get(f"/stats/jobs?days={days}", expect_status=200)
        ledger.equal(f"days={days} 响应字段集 = JobStatsRead", sorted(stats), sorted(STATS_FIELDS))
        ledger.equal(f"days={days} totals 字段集 = StatsTotalsRead", sorted(stats["totals"]), sorted(TOTALS_FIELDS))
        ledger.equal(f"days={days} 回显 days", stats["days"], days)
        ledger.equal(f"days={days} 未传 data_source_id 时回显 null", stats["data_source_id"], None)
        expected = _recompute_stats(jobs_a, tasks_by_job, matches_snapshot, days=days, data_source_id=None)
        if expected["near_edge"]:
            ledger.skip(f"days={days} 窗口边缘 {EDGE_MARGIN_SECONDS}s 带内有作业，重算不可判等",
                        json.dumps([j["id"] for j in expected["near_edge"]]))
            continue
        ledger.equal(f"days={days} totals 与独立重算逐字段一致", stats["totals"], expected["totals"])
        ledger.equal(f"days={days} daily 与独立重算逐行一致", stats["daily"], expected["daily"])
        ledger.equal(f"days={days} top_rules 与独立重算（含并列序）一致", stats["top_rules"], expected["top_rules"])

        daily = stats["daily"]
        if daily:
            ledger.equal("daily 行字段集 = StatsDailyRead", sorted(daily[0]), sorted(DAILY_FIELDS))
            ledger.check("daily 日期升序无重复", daily == sorted(daily, key=lambda r: r["date"])
                         and len({r["date"] for r in daily}) == len(daily), str([r["date"] for r in daily]))
            ledger.check("daily 行数不超过窗口天数", len(daily) <= days, f"rows={len(daily)} days={days}")
            for field, total_field in [
                ("jobs_created", "jobs"), ("files_succeeded", "file_succeeded"),
                ("files_failed", "file_failed"), ("matched_rows", "matched_rows_total"),
                ("bytes_total", "bytes_total"),
            ]:
                ledger.equal(f"daily Σ{field} == totals.{total_field}",
                             sum(row[field] for row in daily), stats["totals"][total_field])
            # 服务端日界只可能落在窗口内：比"桶集合等于作业日集合"更严的独立边界检查。
            # 留一天余量避开跨 UTC 午夜的竞态（cutoff 是服务端算的，与这里差几毫秒）。
            first_allowed = (utcnow() - timedelta(days=days + 1)).date().isoformat()
            last_allowed = utcnow().date().isoformat()
            ledger.check("daily 的每个日期都落在窗口内",
                         all(first_allowed <= row["date"] <= last_allowed for row in daily),
                         f"dates={daily[0]['date']}..{daily[-1]['date']} window={first_allowed}..{last_allowed}")
        top = stats["top_rules"]
        ledger.check(f"days={days} top_rules 不超过 10 条", len(top) <= 10, f"len={len(top)}")
        if top:
            ledger.equal("top_rule 行字段集 = TopRuleRead", sorted(top[0]), sorted(TOP_RULE_FIELDS))
            ledger.check("top_rules 命中数非递增",
                         all(a["hits"] >= b["hits"] for a, b in zip(top, top[1:])), str([r["hits"] for r in top]))
            ledger.check("top_rules 的 (rule_id, rule_version) 唯一",
                         len({(r["rule_id"], r["rule_version"]) for r in top}) == len(top), str(len(top)))
            ledger.check("top_rules 每个桶至少涉及 1 个作业", all(r["jobs"] >= 1 for r in top), str(top[:3]))
            ledger.check("top_rules 命中数之和 ≤ 窗口命中总数",
                         sum(r["hits"] for r in top) <= stats["totals"]["matched_rows_total"],
                         f"Σhits={sum(r['hits'] for r in top)} total={stats['totals']['matched_rows_total']}")
        refetch = api.get(f"/stats/jobs?days={days}", expect_status=200)
        ledger.equal("并列命中数下的 top_rules 次序稳定（不会翻页抖动）", refetch["top_rules"], top)

    stats_all = api.get("/stats/jobs?days=14", expect_status=200)
    stats_ds = api.get(f"/stats/jobs?days=14&data_source_id={ctx['ds_id']}", expect_status=200)
    narrowed = _recompute_stats(jobs_a, tasks_by_job, matches_snapshot, days=14, data_source_id=ctx["ds_id"])
    un = _recompute_stats(jobs_a, tasks_by_job, matches_snapshot, days=14, data_source_id=None)
    if not narrowed["near_edge"]:
        ledger.equal("data_source_id 收窄后 totals 与重算一致", stats_ds["totals"], narrowed["totals"])
        ledger.equal("data_source_id 回显请求值", stats_ds["data_source_id"], ctx["ds_id"])
    ledger.equal("收窄只减不增（jobs）", narrowed["totals"]["jobs"] <= un["totals"]["jobs"], True)
    ledger.equal("收窄只减不增（matched_rows）", narrowed["totals"]["matched_rows_total"] <= un["totals"]["matched_rows_total"], True)
    stats_unknown = api.get("/stats/jobs?days=14&data_source_id=999999", expect_status=200)
    ledger.equal("未知数据源返回空聚合而非报错",
                 (stats_unknown["totals"], stats_unknown["daily"], stats_unknown["top_rules"]),
                 ({f: 0 for f in TOTALS_FIELDS}, [], []))
    stats_days = api.get("/stats/jobs?days=1", expect_status=200)
    ledger.check("days 收窄不会让计数变大",
                 stats_days["totals"]["jobs"] <= stats_all["totals"]["jobs"]
                 and stats_days["totals"]["bytes_total"] <= stats_all["totals"]["bytes_total"],
                 f"d1={stats_days['totals']['jobs']}/{stats_days['totals']['bytes_total']} "
                 f"d14={stats_all['totals']['jobs']}/{stats_all['totals']['bytes_total']}")

    # --- /rules：规则不落新表，索引是查询期把各版本 config 展平，口径全靠这里钉 ---
    for name, query in [
        ("limit=0", "rules?limit=0"),
        ("limit=501", "rules?limit=501"),
        ("offset=-1", "rules?offset=-1"),
        ("type=bogus 越出枚举", "rules?type=bogus"),
    ]:
        expect_error(ledger, f"/rules 参数校验 {name} → 422", lambda q=query: api.get(q), 422)

    full = list_all(api, "/rules", page=500)
    ledger.check("规则索引非空", len(full) > 0, f"rows={len(full)}")
    ledger.equal("规则索引行字段集 = RuleIndexRead", sorted(full[0]), sorted(RULE_FIELDS))
    ledger.check("索引里每行的 type 落在 keyword/regex",
                 {row["type"] for row in full} <= {"keyword", "regex"}, str(sorted({row['type'] for row in full})))
    ledger.check("每行 summary 非空且带类型前缀",
                 all(row["summary"] and (row["summary"].startswith("关键字: ") or row["summary"].startswith("正则: "))
                     for row in full), json.dumps(full[0]["summary"], ensure_ascii=False))

    ours = [row for row in full if row["rule_set_id"] == ctx["rule_set_id"] and row["version"] == ctx["rule_set_version"]]
    ledger.equal("本轮提交的规则集在索引里全量展平", sorted(row["rule_id"] for row in ours), sorted(ctx["rule_ids"]))
    ledger.check("展平保留 severity 字段", {row["severity"] for row in ours} <= {"info", "warning", "error"},
                 str(sorted({row["severity"] for row in ours})))

    # 稳定排序契约：同一规则集名下版本降序（新版本在前），rule_id 升序。
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in full:
        by_name.setdefault(row["rule_set_name"], []).append(row)

    def name_order(rows: list[dict[str, Any]]) -> list[tuple[int, str]]:
        return [(row["version"], row["rule_id"]) for row in rows]

    ledger.check("同名规则集内版本非递增且同版本 rule_id 升序",
                 all(name_order(rows) == sorted(name_order(rows), key=lambda pair: (-pair[0], pair[1]))
                     for rows in by_name.values()), str(sorted(by_name)))
    ledger.check("整体次序是 (名称升序, 版本降序, rule_id 升序)",
                 [(r["rule_set_name"], -r["version"], r["rule_id"]) for r in full] ==
                 sorted((r["rule_set_name"], -r["version"], r["rule_id"]) for r in full), str(len(full)))

    kw = [row for row in full if row["type"] == "keyword"]
    rx = [row for row in full if row["type"] == "regex"]
    ledger.equal("type 过滤是两个互斥分片（keyword + regex = 全量）", len(kw) + len(rx), len(full))
    ledger.check("type=keyword 只回 keyword 行",
                 all(row["type"] == "keyword" for row in api.get("/rules?type=keyword&limit=500")), str(len(kw)))
    ledger.check("type=regex 只回 regex 行",
                 all(row["type"] == "regex" for row in api.get("/rules?type=regex&limit=500")), str(len(rx)))

    hit_err = api.get("/rules?keyword=ERROR&limit=500", expect_status=200)
    ledger.check("keyword 命中的是规则内容而非列名", len(hit_err) > 0, f"rows={len(hit_err)}")
    ledger.equal("keyword 大小写不敏感（子串按小写比较）",
                 api.get("/rules?keyword=error&limit=500"), hit_err)
    # 展平后的 keyword 匹配是 Python 子串比较，不是 LIKE：ERR_R 若被当 LIKE 会命中 ERROR，
    # 按字面量子串则必须落空；% 同理（LIKE 下会全表命中）。
    ledger.equal("keyword=ERR_R 未被当成 LIKE 通配（无字面 ERR_R 即空）",
                 api.get("/rules?keyword=ERR_R&limit=500", expect_status=200), [])
    ledger.equal("keyword=%25（字面量百分号）匹配为空", api.get("/rules?keyword=%25&limit=500", expect_status=200), [])

    wildcard = api.get("/rules?rule_set_name=%25&limit=500", expect_status=200)
    ledger.equal("rule_set_name 走转义 LIKE：% 不是通配符", wildcard, [])
    underscore = api.get("/rules?rule_set_name=_&limit=500", expect_status=200)
    ledger.check("rule_set_name=_ 只回真正含下划线的名字（未转义会全表命中）",
                 all("_" in row["rule_set_name"] for row in underscore) and len(underscore) < len(full),
                 f"rows={len(underscore)} names={sorted({r['rule_set_name'] for r in underscore})}")
    named = api.get("/rules?rule_set_name=e2e-canonical&limit=500", expect_status=200)
    ledger.check("rule_set_name 子串过滤只回该名字的行",
                 len(named) > 0 and {row["rule_set_name"] for row in named} == {"e2e-canonical"}, f"rows={len(named)}")
    ledger.equal("rule_set_name=e2e%canonical 转义后不匹配连字符名",
                 api.get("/rules?rule_set_name=e2e%25canonical&limit=500", expect_status=200), [])

    small = api.get("/rules?limit=7&offset=0", expect_status=200)
    rest = api.get(f"/rules?limit=7&offset={len(small)}", expect_status=200)
    ledger.equal("offset 翻页拼起来等于全量次序的前缀", small + rest, full[:len(small) + len(rest)])
    ledger.equal("limit 生效", len(small), 7)
    tail = api.get(f"/rules?limit=500&offset={max(0, len(full) - 2)}", expect_status=200)
    ledger.equal("越界 offset 回空数组而不是报错", api.get(f"/rules?limit=50&offset={len(full) + 10}", expect_status=200), [])
    ledger.equal("尾部翻页与全量尾部一致", tail, full[-len(tail):])


# ============================================================================
# 入口
# ============================================================================
PHASES = ("cancel", "drift", "stats")
RUNNERS = {"cancel": phase_cancel, "drift": phase_drift, "stats": phase_stats}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LogHarvest 运行时流程：取消 / 漂移 / 只读聚合")
    parser.add_argument("--phase", default="all", help=f"逗号分隔：{','.join(PHASES)} 或 all")
    args = parser.parse_args(argv)
    wanted = PHASES if args.phase == "all" else tuple(p.strip() for p in args.phase.split(","))
    unknown = set(wanted) - set(PHASES)
    if unknown:
        print(f"!! 未知阶段：{sorted(unknown)}，可选 {list(PHASES)}", flush=True)
        return 2

    ledger = Ledger("Runtime Flow")
    api = ApiClient(base=API_BASE)
    try:
        ctx = bootstrap(ledger, api)
    except (ApiError, TimeoutError, KeyError, OSError) as exc:
        ledger.fail_phase("R0 前置阶段异常", exc)
        print(f"!! 前置中断：{type(exc).__name__}: {exc}", flush=True)
        return 1
    for name in wanted:
        if name not in RUNNERS:
            ledger.check(f"阶段 {name} 存在", False, str(sorted(RUNNERS)))
            continue
        try:
            RUNNERS[name](ledger, api, ctx)
        except (ApiError, TimeoutError, KeyError, OSError) as exc:
            ledger.fail_phase(f"{name} 阶段异常", exc)
            print(f"!! 阶段 {name} 中断：{type(exc).__name__}: {exc}", flush=True)
    summary = ledger.summary()
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
