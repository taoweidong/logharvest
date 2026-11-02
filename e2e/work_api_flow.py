"""Work 功能（作业 / Worker）全量接口测试。

覆盖面与口径：
- 正常参数：真实 NAS 目录建作业、Worker 真实执行、与夹具 oracle 对账。
- 边界值：limit/offset/max_events/max_depth/max_files 的上下界。
- 异常参数：422/400/404/409/401/403 的完整错误语义。
- RBAC：viewer 只读、operator 可操作的真实角色账号。
- 幂等键：重放 200 / 冲突 409 / 空白 400。

离线 Worker 与失败任务两个状态无法从外部 API 造出来（Worker 心跳会立刻改回去），
用 psql 直写 workers/file_tasks 模拟，脚本里显式标注 SIMULATED 并在结束时恢复。

用法：python e2e/work_api_flow.py
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from typing import Any

from client import (
    API_BASE,
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    TERMINAL_JOB_STATUSES,
    ApiClient,
    Ledger,
    load_fixtures,
    load_ruleset_payload,
)

LEDGER = Ledger("Work API Flow")
FIXTURES = load_fixtures()


def psql(sql: str) -> str:
    """直连 e2e 库执行 SQL（只用于模拟外部 API 造不出的状态）。"""
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    out = subprocess.run(
        ["wslc", "exec", "lh-e2e-pg", "psql", "-U", "logharvest",
         "-d", "logharvest_e2e", "-tAc", sql],
        capture_output=True, text=True, timeout=30, env=env, check=True,
    )
    return out.stdout.strip()


def expect_error(
    ledger: Ledger,
    name: str,
    fn,
    status: int,
    *,
    contains: str | None = None,
) -> Any:
    """断言 fn 抛出指定状态码（可选 detail 包含片段），返回实际响应体。

    SSE 走裸 urllib（不抛 ApiError），这里一并把 urllib.error.HTTPError
    归一成按 ``exc.code`` 判定的错误。
    """
    import urllib.error

    from client import ApiError

    try:
        fn()
    except ApiError as exc:
        ok = exc.status == status
        if ok and contains is not None:
            detail = exc.detail
            text = json.dumps(detail, ensure_ascii=False) if not isinstance(detail, str) else detail
            ok = contains in text
        ledger.check(name, ok, f"HTTP {exc.status}: {json.dumps(exc.body, ensure_ascii=False)[:200]}")
        return exc.body
    except urllib.error.HTTPError as exc:
        ledger.check(name, exc.code == status, f"HTTP {exc.code}（urllib 直连）")
        return None
    except Exception as exc:  # noqa: BLE001
        ledger.check(name, False, f"意外异常 {type(exc).__name__}: {exc}")
        return None
    ledger.check(name, False, "请求意外成功（应当报错）")
    return None


# ---------------------------------------------------------------- W0 认证
def phase_auth(ledger: Ledger) -> ApiClient:
    ledger.phase("W0 认证与匿名访问")
    anon = ApiClient()
    expect_error(ledger, "无令牌访问作业列表 → 401", lambda: anon.get("/jobs"), 401)
    bad = ApiClient(token="not-a-real-token")
    expect_error(ledger, "伪造令牌 → 401", lambda: bad.get("/jobs"), 401)

    wrong = ApiClient()
    expect_error(
        ledger, "错误口令登录 → 401",
        lambda: wrong.post("/login", {"username": ADMIN_USERNAME, "password": "wrong-password"}),
        401,
    )

    api = ApiClient()
    api.login(ADMIN_USERNAME, ADMIN_PASSWORD)
    ledger.check("管理员登录拿到 accessToken", bool(api.token))
    return api


# ---------------------------------------------------------------- W1 准备
def phase_prepare(ledger: Ledger, api: ApiClient) -> dict[str, int]:
    ledger.phase("W1 数据源与规则集准备")
    ds_name = f"work-flow-{uuid.uuid4().hex[:8]}"
    ds = api.post(
        "/data-sources",
        {"name": ds_name, "root_path": "/nas", "read_only": True,
         "copy_to_local": True, "description": "work 功能接口测试"},
        expect_status=201,
    )
    ledger.check("数据源创建 201", ds["id"] > 0, json.dumps(ds, ensure_ascii=False)[:200])

    expect_error(
        ledger, "同名数据源 → 409",
        lambda: api.post("/data-sources", {"name": ds_name, "root_path": "/nas"}), 409,
    )
    expect_error(
        ledger, "root_path 为空 → 422",
        lambda: api.post("/data-sources", {"name": f"bad-{uuid.uuid4().hex[:6]}", "root_path": ""}),
        422,
    )

    test = api.post(f"/data-sources/{ds['id']}/test", None, expect_status=200)
    ledger.check(
        "数据源测试：/nas 存在、是目录、可读",
        test["exists"] and test["is_dir"] and test["readable"], json.dumps(test),
    )

    canonical_config = load_ruleset_payload("canonical_ruleset.json")
    canonical = {"name": canonical_config["name"], "config": canonical_config}
    rs = api.post("/rule-sets", canonical, expect_status=201)
    ledger.check("规则集创建 201（version≥1）", rs["version"] >= 1, str(rs))
    rs_again = api.post("/rule-sets", canonical, expect_status=201)
    ledger.check(
        "同名再提交：服务端自增版本（契约 §5）",
        rs_again["version"] == rs["version"] + 1 and rs_again["id"] != rs["id"],
        json.dumps(rs_again, ensure_ascii=False)[:160],
    )
    bad_rule = {
        "name": f"bad-{uuid.uuid4().hex[:6]}",
        "config": {"name": "k", "rules": [{
            "id": "x", "name": "x", "enabled": True, "severity": "error",
            "match": {"type": "keyword", "any": ["x"]},
            "threshold": {"field": "ms", "op": "gt", "value": 1},
        }]},
    }
    expect_error(ledger, "keyword 配 threshold → 422", lambda: api.post("/rule-sets", bad_rule), 422)
    return {"ds": ds["id"], "rs": rs["id"]}


# ---------------------------------------------------------------- W2 创建契约
def phase_create_contract(ledger: Ledger, api: ApiClient, ids: dict[str, int]) -> dict[str, Any]:
    ledger.phase("W2 作业创建契约（正常/幂等/异常）")
    ds_id, rs_id = ids["ds"], ids["rs"]

    key1 = uuid.uuid4().hex
    job = api.post(
        "/jobs",
        {"name": "work-clean", "data_source_id": ds_id, "source_path": "/nas/clean",
         "rule_set_id": rs_id, "file_filter": {"recursive": True, "max_depth": 5, "max_files": 20000, "suffixes": None}},
        headers={"Idempotency-Key": key1}, expect_status=201,
    )
    ledger.check("clean 建作业 201：total_files=2", job["total_files"] == 2, json.dumps(job, ensure_ascii=False))
    ledger.check("clean 建作业：created_by=admin", job["created_by"] == ADMIN_USERNAME)
    ledger.check("clean 建作业：初始 running", job["status"] == "running")

    replay = api.post(
        "/jobs",
        {"name": "work-clean", "data_source_id": ds_id, "source_path": "/nas/clean",
         "rule_set_id": rs_id, "file_filter": {"recursive": True, "max_depth": 5, "max_files": 20000, "suffixes": None}},
        headers={"Idempotency-Key": key1}, expect_status=200,
    )
    ledger.equal("同键同体重放 → 200 且同一作业", replay["id"], job["id"])

    expect_error(
        ledger, "同键不同体 → 409",
        lambda: api.post(
            "/jobs",
            {"name": "work-clean-renamed", "data_source_id": ds_id, "source_path": "/nas/clean",
             "rule_set_id": rs_id},
            headers={"Idempotency-Key": key1},
        ),
        409,
    )
    expect_error(
        ledger, "空白 Idempotency-Key → 400",
        lambda: api.post("/jobs", {"name": "blank-key", "data_source_id": ds_id,
                                   "source_path": "/nas/clean", "rule_set_id": rs_id},
                         headers={"Idempotency-Key": "   "}),
        400,
    )
    no_key = api.post("/jobs", {"name": "work-no-key", "data_source_id": ds_id,
                                "source_path": "/nas/clean", "rule_set_id": rs_id}, expect_status=201)
    ledger.check("不带幂等键 → 201 新作业", no_key["id"] != job["id"])

    expect_error(ledger, "数据源不存在 → 404",
                 lambda: api.post("/jobs", {"name": "x", "data_source_id": 999999,
                                            "source_path": "/nas/clean", "rule_set_id": rs_id}), 404)
    expect_error(ledger, "规则集不存在 → 404",
                 lambda: api.post("/jobs", {"name": "x", "data_source_id": ds_id,
                                            "source_path": "/nas/clean", "rule_set_id": 999999}), 404)
    expect_error(ledger, "目录越出 root → 400",
                 lambda: api.post("/jobs", {"name": "x", "data_source_id": ds_id,
                                            "source_path": "/etc", "rule_set_id": rs_id}), 400)
    expect_error(ledger, "目录不存在 → 400",
                 lambda: api.post("/jobs", {"name": "x", "data_source_id": ds_id,
                                            "source_path": "/nas/no-such-dir", "rule_set_id": rs_id}), 400)
    expect_error(ledger, "目录是文件 → 400",
                 lambda: api.post("/jobs", {"name": "x", "data_source_id": ds_id,
                                            "source_path": "/nas/clean/app.zip", "rule_set_id": rs_id}), 400)
    expect_error(ledger, "作业名为空 → 422",
                 lambda: api.post("/jobs", {"name": "", "data_source_id": ds_id,
                                            "source_path": "/nas/clean", "rule_set_id": rs_id}), 422)
    for field, value in (("max_depth", 0), ("max_depth", 21), ("max_files", 0), ("max_files", 20001)):
        expect_error(
            ledger, f"file_filter.{field}={value} → 422",
            lambda field=field, value=value: api.post(
                "/jobs",
                {"name": "x", "data_source_id": ds_id, "source_path": "/nas/clean",
                 "rule_set_id": rs_id, "file_filter": {field: value}},
            ),
            422,
        )
    expect_error(
        ledger, "全不合法后缀 → 400",
        lambda: api.post("/jobs", {"name": "x", "data_source_id": ds_id, "source_path": "/nas/clean",
                                   "rule_set_id": rs_id, "file_filter": {"suffixes": [".txt", ".doc"]}}),
        400,
    )
    expect_error(
        ledger, "max_files=1 扫描 6 个文件 → 400",
        lambda: api.post("/jobs", {"name": "x", "data_source_id": ds_id, "source_path": "/nas/formats",
                                   "rule_set_id": rs_id, "file_filter": {"max_files": 1}}),
        400,
    )

    gz_job = api.post(
        "/jobs", {"name": "work-suffix-gz", "data_source_id": ds_id, "source_path": "/nas/formats",
                  "rule_set_id": rs_id, "file_filter": {"suffixes": [".gz"]}}, expect_status=201,
    )
    ledger.equal("合法后缀过滤：formats 只剩 .gz", gz_job["total_files"], 1)
    deep_job = api.post(
        "/jobs", {"name": "work-depth3", "data_source_id": ds_id, "source_path": "/nas/deep",
                  "rule_set_id": rs_id, "file_filter": {"max_depth": 3}}, expect_status=201,
    )
    ledger.equal("max_depth=3：deep 只发现前 3 层", deep_job["total_files"], 3)
    empty_job = api.post(
        "/jobs", {"name": "work-empty", "data_source_id": ds_id, "source_path": "/nas/empty",
                  "rule_set_id": rs_id}, expect_status=201,
    )
    ledger.check(
        "空目录作业：直接 succeeded（0 文件）",
        empty_job["status"] == "succeeded" and empty_job["total_files"] == 0
        and empty_job["finished_at"] is not None,
        json.dumps(empty_job, ensure_ascii=False),
    )
    return {"clean": job, "no_key": no_key, "gz": gz_job, "deep": deep_job, "empty": empty_job}


# ---------------------------------------------------------------- W3 查询
def phase_query(ledger: Ledger, api: ApiClient, jobs: dict[str, Any]) -> None:
    ledger.phase("W3 作业/文件任务查询与分页边界")
    listing = api.get("/jobs", expect_status=200)
    ledger.check("作业列表是裸数组", isinstance(listing, list) and len(listing) >= 1)
    created = [j["created_at"] for j in listing]
    ledger.check("按创建时间倒序", created == sorted(created, reverse=True))

    ledger.equal("limit=1 只回 1 条", len(api.get("/jobs?limit=1")), 1)
    expect_error(ledger, "limit=0 → 422", lambda: api.get("/jobs?limit=0"), 422)
    expect_error(ledger, "limit=501 → 422", lambda: api.get("/jobs?limit=501"), 422)
    api.get("/jobs?limit=500", expect_status=200)
    ledger.check("limit=500 合法", True)
    expect_error(ledger, "offset=-1 → 422", lambda: api.get("/jobs?offset=-1"), 422)
    ledger.equal("offset 超界 → 空数组", api.get("/jobs?offset=99999"), [])

    running = api.get("/jobs?status=running", expect_status=200)
    ledger.check("status=running 全部匹配", all(j["status"] == "running" for j in running))
    expect_error(ledger, "status=非法值 → 422",
                 lambda: api.get("/jobs?status=bogus"), 422)

    expect_error(ledger, "作业详情不存在 → 404", lambda: api.get("/jobs/999999"), 404)
    expect_error(ledger, "作业详情 id 非整数 → 422", lambda: api.get("/jobs/abc"), 422)

    clean = jobs["clean"]
    files = api.get(f"/jobs/{clean['id']}/files", expect_status=200)
    ledger.equal("文件任务列表=total_files", len(files), clean["total_files"])
    ledger.check("文件任务字段齐全",
                 {"id", "job_id", "file_name", "size_bytes", "status", "attempts", "max_attempts"} <= set(files[0]),
                 json.dumps(files[0], ensure_ascii=False)[:200])
    one = api.get(f"/jobs/{clean['id']}/files?limit=1", expect_status=200)
    ledger.equal("文件任务 limit=1", len(one), 1)
    expect_error(ledger, "文件任务 status 非法 → 422",
                 lambda: api.get(f"/jobs/{clean['id']}/files?status=nope"), 422)
    expect_error(ledger, "文件任务列表作业不存在 → 404", lambda: api.get("/jobs/999999/files"), 404)

    task_id = files[0]["id"]
    attempts = api.get(f"/jobs/{clean['id']}/files/{task_id}/attempts", expect_status=200)
    ledger.check("尝试历史是数组（可能尚为空）", isinstance(attempts, list))
    expect_error(ledger, "尝试历史任务不存在 → 404",
                 lambda: api.get(f"/jobs/{clean['id']}/files/999999/attempts"), 404)
    other = jobs["no_key"]
    other_files = api.get(f"/jobs/{other['id']}/files", expect_status=200)
    expect_error(ledger, "尝试历史跨作业归属 → 404",
                 lambda: api.get(f"/jobs/{other['id']}/files/{task_id}/attempts"), 404)
    ledger.check("跨作业任务 id 备好", isinstance(other_files, list))


# ---------------------------------------------------------------- W4 真实执行
def phase_harvest(ledger: Ledger, api: ApiClient, jobs: dict[str, Any]) -> None:
    ledger.phase("W4 Worker 真实执行与 oracle 对账")
    clean = jobs["clean"]
    final = api.wait_job_terminal(clean["id"], timeout=180)
    ledger.equal("clean 作业终态 succeeded", final["status"], "succeeded")

    files = api.get(f"/jobs/{clean['id']}/files", expect_status=200)
    ledger.check("全部文件任务 succeeded", all(f["status"] == "succeeded" for f in files),
                 json.dumps([f["status"] for f in files]))
    ledger.equal("已处理行数对账（26）", sum(f["processed_lines"] for f in files),
                 FIXTURES["cases"]["clean"]["expect_total_lines"])
    ledger.equal("命中行数对账（14）", sum(f["matched_rows"] for f in files),
                 FIXTURES["cases"]["clean"]["expect_total_matches"])

    attempts = api.get(f"/jobs/{clean['id']}/files/{files[0]['id']}/attempts", expect_status=200)
    ledger.check("尝试历史 ≥1 条", len(attempts) >= 1)
    first = attempts[0]
    ledger.check("尝试字段：worker_id/版本/状态/起止时间",
                 first["worker_id"] and first["worker_version"] == "0.1.0"
                 and first["status"] == "succeeded" and first["started_at"] and first["finished_at"],
                 json.dumps(first, ensure_ascii=False)[:300])
    nos = [a["attempt_no"] for a in attempts]
    ledger.check("attempt_no 升序", nos == sorted(nos))

    matches = api.get(f"/matches?job_id={clean['id']}&limit=500", expect_status=200)
    ledger.equal("命中结果行数对账（14）", len(matches), FIXTURES["cases"]["clean"]["expect_total_matches"])
    by_rule: dict[str, int] = {}
    for m in matches:
        by_rule[m["rule_id"]] = by_rule.get(m["rule_id"], 0) + 1
    oracle_by_rule = FIXTURES["cases"]["clean"]["expect_matches_by_rule"]
    ledger.check("按规则命中数对账", by_rule == oracle_by_rule,
                 f"actual={by_rule} expected={oracle_by_rule}")

    for name in ("gz", "deep"):
        final = api.wait_job_terminal(jobs[name]["id"], timeout=180)
        ledger.check(f"{name} 作业终态 succeeded", final["status"] == "succeeded", final["status"])

    empty = api.get(f"/jobs/{jobs['empty']['id']}", expect_status=200)
    ledger.check("空目录作业保持 succeeded", empty["status"] == "succeeded")


# ---------------------------------------------------------------- W5 取消
def phase_cancel(ledger: Ledger, api: ApiClient, ids: dict[str, int]) -> int:
    ledger.phase("W5 取消与重试约束")
    ds_id, rs_id = ids["ds"], ids["rs"]
    key = uuid.uuid4().hex
    big = api.post("/jobs", {"name": "work-cancel", "data_source_id": ds_id,
                             "source_path": "/nas/cancel", "rule_set_id": rs_id},
                   headers={"Idempotency-Key": key}, expect_status=201)
    cancelled = api.post(f"/jobs/{big['id']}/cancel", None, expect_status=200)
    ledger.check(
        "取消后 cancel_requested=true",
        cancelled["cancel_requested"] is True, json.dumps(cancelled, ensure_ascii=False)[:200],
    )
    ledger.check("取消后进入终态或标记取消",
                 cancelled["status"] in TERMINAL_JOB_STATUSES, cancelled["status"])
    again = api.post(f"/jobs/{big['id']}/cancel", None, expect_status=200)
    ledger.equal("终态作业再取消：幂等不变", again["status"], cancelled["status"])
    expect_error(ledger, "取消不存在的作业 → 404", lambda: api.post("/jobs/999999/cancel"), 404)
    expect_error(
        ledger, "已取消作业重试失败任务 → 409",
        lambda: api.post(f"/jobs/{big['id']}/files/retry-failed"), 409,
    )
    files = api.get(f"/jobs/{big['id']}/files", expect_status=200)
    ledger.check("pending 任务被置为 cancelled",
                 all(f["status"] in ("cancelled", "running", "succeeded", "partial") for f in files),
                 json.dumps([f["status"] for f in files]))
    return big["id"]


# ---------------------------------------------------------------- W6 重试（SIMULATED）
def phase_retry(ledger: Ledger, api: ApiClient, clean_job_id: int) -> None:
    ledger.phase("W6 失败任务重试（SQL 模拟 failed 后走真实 API）")
    files = api.get(f"/jobs/{clean_job_id}/files", expect_status=200)
    victim = files[0]["id"]
    psql(f"UPDATE file_tasks SET status='failed', error='simulated-for-retry-test' WHERE id={victim}")
    # 说明：作业状态只在 Worker/服务端真正推进时联动；SQL 直改任务失败不会回写作
    # 业行（服务端从未见过这次失败），所以这里只要求作业未处于 cancelled。
    got = api.get(f"/jobs/{clean_job_id}", expect_status=200)
    ledger.check("SQL 直改失败后作业未被误取消", got["status"] != "cancelled", got["status"])

    result = api.post(f"/jobs/{clean_job_id}/files/retry-failed", None, expect_status=200)
    ledger.check("重试返回 retried=1 remaining=0",
                 result == {"retried": 1, "remaining_failed": 0}, json.dumps(result))

    deadline = time.monotonic() + 120
    status = "pending"
    while time.monotonic() < deadline:
        row = [f for f in api.get(f"/jobs/{clean_job_id}/files", expect_status=200) if f["id"] == victim]
        status = row[0]["status"]
        if status in ("succeeded", "partial", "failed"):
            break
        time.sleep(2)
    ledger.check("重试任务被 Worker 重新领取并成功", status == "succeeded", status)
    attempts = api.get(f"/jobs/{clean_job_id}/files/{victim}/attempts", expect_status=200)
    ledger.check("尝试历史累计 2 次", len(attempts) >= 2, str(len(attempts)))
    final = api.wait_job_terminal(clean_job_id, timeout=120)
    ledger.equal("作业回到终态 succeeded", final["status"], "succeeded")

    none = api.post(f"/jobs/{clean_job_id}/files/retry-failed", None, expect_status=200)
    ledger.check("无失败任务重试 → {0,0}", none == {"retried": 0, "remaining_failed": 0}, json.dumps(none))
    expect_error(ledger, "重试不存在的作业 → 404",
                 lambda: api.post("/jobs/999999/files/retry-failed"), 404)


# ---------------------------------------------------------------- W7 Worker
def phase_workers(ledger: Ledger, api: ApiClient) -> list[str]:
    ledger.phase("W7 Worker 接口（列表/排空/恢复/按版本）")
    workers = api.get("/workers", expect_status=200)
    ids = [w["id"] for w in workers]
    # 在册行可能多于两台：其它套件（api_flow P7）会注册 worker-3 这类"注册后
    # 不再心跳"的离线行。存活口径只看非 offline 的行。
    alive = [w for w in workers if w["status"] != "offline"]
    ledger.check("两台 e2e 容器 Worker 在册且存活",
                 all(any(w["id"] == f"lh-e2e-worker-{i}" and w["status"] != "offline"
                         for w in workers) for i in (1, 2)),
                 json.dumps(ids))
    ledger.check("Worker 字段齐全",
                 {"id", "hostname", "version", "status", "drain_requested", "last_heartbeat"} <= set(workers[0]),
                 json.dumps(workers[0], ensure_ascii=False)[:300])
    expect_error(ledger, "limit=0 → 422", lambda: api.get("/workers?limit=0"), 422)
    expect_error(ledger, "limit=201 → 422", lambda: api.get("/workers?limit=201"), 422)
    expect_error(ledger, "offset=-1 → 422", lambda: api.get("/workers?offset=-1"), 422)

    target = alive[0]["id"]
    drained = api.post(f"/workers/{target}/drain", None, expect_status=200)
    ledger.check("排空：drain_requested=true 且 draining",
                 drained["drain_requested"] is True and drained["status"] == "draining",
                 json.dumps(drained))
    resumed = api.post(f"/workers/{target}/resume", None, expect_status=200)
    ledger.check("恢复：drain_requested=false 且 online",
                 resumed["drain_requested"] is False and resumed["status"] in ("online", "busy"),
                 json.dumps(resumed))
    expect_error(ledger, "排空不存在的 Worker → 404",
                 lambda: api.post("/workers/no-such-worker/drain"), 404)
    expect_error(ledger, "恢复不存在的 Worker → 404",
                 lambda: api.post("/workers/no-such-worker/resume"), 404)
    if any(w["status"] == "offline" for w in workers):
        offline_row = next(w["id"] for w in workers if w["status"] == "offline")
        expect_error(ledger, "遗留离线 Worker 恢复 → 409（心跳冻结行不被复活）",
                     lambda: api.post(f"/workers/{offline_row}/resume"), 409)

    version = alive[0]["version"]
    dv = api.post(f"/workers/drain-version?version={version}", None, expect_status=200)
    ledger.check("按版本排空 drained≥1", dv["drained"] + dv["flagged"] >= 1, json.dumps(dv))
    for wid in (w["id"] for w in alive):
        api.post(f"/workers/{wid}/resume", None, expect_status=200)
    dv2 = api.post("/workers/drain-version?version=no-such-version", None, expect_status=200)
    ledger.check("未知版本排空 {0,0}", dv2 == {"drained": 0, "flagged": 0}, json.dumps(dv2))
    expect_error(ledger, "按版本排空缺 version → 422",
                 lambda: api.post("/workers/drain-version"), 422)

    # SIMULATED：离线状态只能直写库（真实 Worker 心跳会立刻覆盖）。
    # 挑一台存活 Worker 临时置离线，结束后按其原状态恢复。
    sim = alive[-1]
    sim_id, original_status = sim["id"], sim["status"]
    psql(f"UPDATE workers SET status='offline' WHERE id='{sim_id}'")
    try:
        expect_error(ledger, "离线 Worker 恢复 → 409",
                     lambda: api.post(f"/workers/{sim_id}/resume"), 409)
        off = api.post(f"/workers/{sim_id}/drain", None, expect_status=200)
        ledger.check("离线 Worker 排空：状态保持 offline 只记标记",
                     off["status"] == "offline" and off["drain_requested"] is True, json.dumps(off))
    finally:
        psql(f"UPDATE workers SET status='{original_status}', drain_requested=false WHERE id='{sim_id}'")
        api.post(f"/workers/{sim_id}/resume", None, expect_status=200)
    return [w["id"] for w in alive]


# ---------------------------------------------------------------- W8 SSE
def phase_sse(ledger: Ledger, api: ApiClient, terminal_job_id: int, running_job_id: int) -> None:
    ledger.phase("W8 SSE 实时进度流")
    frames = list(api.job_events(terminal_job_id, max_events=5))
    ledger.check("终态作业 SSE 恰好 1 帧", len(frames) == 1, f"{len(frames)} 帧")
    ledger.check("终态快照状态正确", frames and frames[0]["status"] == "succeeded",
                 json.dumps(frames[:1]))
    expect_error(ledger, "SSE 未知作业 → 404",
                 lambda: list(api.job_events(999999, max_events=1)), 404)
    anon = ApiClient()
    expect_error(ledger, "SSE 无令牌 → 401",
                 lambda: list(anon.job_events(terminal_job_id, max_events=1)), 401)
    capped = list(api.job_events(running_job_id, max_events=1))
    ledger.equal("max_events=1 封顶", len(capped), 1)
    import urllib.error
    import urllib.request

    def raw_events(path: str) -> int:
        req = urllib.request.Request(f"{API_BASE}{path}",
                                     headers={"Authorization": f"Bearer {api.token}",
                                              "Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status

    try:
        raw_events("/jobs/1/events?max_events=-1")
        ledger.check("max_events=-1 → 422", False, "请求意外成功")
    except urllib.error.HTTPError as exc:
        ledger.check("max_events=-1 → 422", exc.code == 422, f"HTTP {exc.code}")
    try:
        raw_events("/jobs/1/events?max_events=101")
        ledger.check("max_events=101 → 422", False, "请求意外成功")
    except urllib.error.HTTPError as exc:
        ledger.check("max_events=101 → 422", exc.code == 422, f"HTTP {exc.code}")


# ---------------------------------------------------------------- W9 RBAC
def phase_rbac(ledger: Ledger, api: ApiClient, ids: dict[str, int], worker_ids: list[str]) -> None:
    ledger.phase("W9 RBAC：viewer 只读 / operator 可操作")
    roles = api.get("/roles", expect_status=200)
    role_map = {r["code"]: r["id"] for r in roles}
    ledger.check("内置角色在册", {"admin", "operator", "viewer"} <= set(role_map), json.dumps(role_map))

    viewer_token = None
    operator_token = None
    suffix = uuid.uuid4().hex[:6]
    v = api.post("/users", {"username": f"wview{suffix}", "password": "Wview#2026pass",
                            "nickname": "work-viewer", "role_ids": [role_map["viewer"]]},
                 expect_status=201)
    o = api.post("/users", {"username": f"woper{suffix}", "password": "Woper#2026pass",
                            "nickname": "work-operator", "role_ids": [role_map["operator"]]},
                 expect_status=201)
    vc = ApiClient()
    vc.login(f"wview{suffix}", "Wview#2026pass")
    viewer_token = vc.token
    oc = ApiClient()
    oc.login(f"woper{suffix}", "Woper#2026pass")
    operator_token = oc.token
    ledger.check("viewer/operator 登录成功", bool(viewer_token) and bool(operator_token))

    vc.get("/jobs", expect_status=200)
    ledger.check("viewer 可读作业列表", True)
    expect_error(
        ledger, "viewer 建作业 → 403",
        lambda: vc.post("/jobs", {"name": "v", "data_source_id": ids["ds"],
                                  "source_path": "/nas/clean", "rule_set_id": ids["rs"]}), 403,
    )
    expect_error(ledger, "viewer 取消作业 → 403",
                 lambda: vc.post("/jobs/1/cancel"), 403)
    expect_error(ledger, "viewer 排空 Worker → 403",
                 lambda: vc.post(f"/workers/{worker_ids[0]}/drain"), 403)

    vjob = oc.post("/jobs", {"name": "oper-job", "data_source_id": ids["ds"],
                             "source_path": "/nas/clean", "rule_set_id": ids["rs"]}, expect_status=201)
    ledger.check("operator 可建作业", vjob["status"] in ("running", "succeeded"))
    od = oc.post(f"/workers/{worker_ids[0]}/drain", None, expect_status=200)
    ledger.check("operator 可排空 Worker", od["drain_requested"] is True)
    oc.post(f"/workers/{worker_ids[0]}/resume", None, expect_status=200)
    # 种子授权（rbac_seed.py OPERATOR_MENUS）：operator 含 AuditLogs+audit:read，
    # viewer 不含——正确口径是 operator 能看、viewer 不能看。
    oc.get("/operation-logs?limit=10", expect_status=200)
    ledger.check("operator 可读操作日志（audit:read 已授权）", True)
    expect_error(ledger, "viewer 读操作日志 → 403",
                 lambda: vc.get("/operation-logs?limit=10"), 403)
    # 收尾：取消 operator 建的作业，避免遗留运行中作业
    oc.post(f"/jobs/{vjob['id']}/cancel", None, expect_status=200)


# ---------------------------------------------------------------- W10 一致性
def phase_consistency(ledger: Ledger, api: ApiClient, clean_job_id: int) -> None:
    ledger.phase("W10 系统状态与审计一致性")
    overview = api.get("/system/overview", expect_status=200)
    workers = api.get("/workers", expect_status=200)
    alive = sum(1 for w in workers if w["status"] in ("online", "busy", "draining"))
    ledger.equal("overview.workers.online 与列表一致", overview["workers"]["online"], alive)
    jobs = api.get("/jobs?limit=500", expect_status=200)
    by_status: dict[str, int] = {}
    for j in jobs:
        by_status[j["status"]] = by_status.get(j["status"], 0) + 1
    ledger.check("overview.jobs 计数包含所有作业状态",
                 all(overview["jobs"].get(k, 0) >= v for k, v in by_status.items()),
                 f"overview={overview['jobs']} local={by_status}")

    logs = api.get("/operation-logs?limit=100", expect_status=200)
    actions = {log["action"] for log in logs}
    need = {"job.create", "job.cancel", "job.retry_failed", "worker.drain", "worker.resume"}
    missing = need - actions
    ledger.check("审计留痕覆盖 work 操作", not missing, f"缺少 {missing}，实际 {sorted(actions)[:20]}")


# ---------------------------------------------------------------- 清理
def phase_cleanup(ledger: Ledger, api: ApiClient) -> None:
    ledger.phase("W11 清理：删除本脚本创建的测试用户")
    # 本脚本创建的用户名前缀 wview/woper（W9 阶段）。不清理会污染 ui_flow 的
    # 「用户列表回到内置三行」绝对计数断言；顺手扫掉历史运行的同前缀残留。
    users = api.get("/users?limit=200", expect_status=200)
    removed = 0
    for u in users:
        if u["username"].startswith(("wview", "woper")):
            api.delete(f"/users/{u['id']}", expect_status=204)
            removed += 1
    left = [u["username"] for u in api.get("/users?limit=200", expect_status=200)
            if u["username"].startswith(("wview", "woper"))]
    ledger.check("测试用户清零（本轮删除且无残留）", removed > 0 and not left,
                 f"removed={removed} left={left}")
    ledger.check("内置 admin 账号仍在", any(u["username"] == "admin" for u in
                 api.get("/users?limit=200", expect_status=200)))


def main() -> int:
    ledger = LEDGER
    try:
        api = phase_auth(ledger)
        ids = phase_prepare(ledger, api)
        jobs = phase_create_contract(ledger, api, ids)
        phase_query(ledger, api, jobs)
        phase_harvest(ledger, api, jobs)
        phase_cancel(ledger, api, ids)
        phase_retry(ledger, api, jobs["clean"]["id"])
        worker_ids = phase_workers(ledger, api)
        terminal_id = jobs["empty"]["id"]
        phase_sse(ledger, api, terminal_id, jobs["clean"]["id"])
        phase_rbac(ledger, api, ids, worker_ids)
        phase_consistency(ledger, api, jobs["clean"]["id"])
        phase_cleanup(ledger, api)
    except Exception as exc:  # noqa: BLE001
        ledger.fail_phase("未捕获异常（流程中断）", exc)
    summary = ledger.summary()
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
