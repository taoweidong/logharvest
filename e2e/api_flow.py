"""LogHarvest API 全流程自动化测试（真实 wslc 栈，无任何 mock）。

对每一阶段的请求/响应形状、错误码与文案都取自 e2e/refs/api-contract.md（逐字核对过
file:line），数值期望取自 e2e/artifacts/fixtures.json（由 e2e/make_nas.py 的内建迷你
规则引擎推导，夹具与期望同源，不会漂）。

运行：
    uv run --python 3.12 python e2e/api_flow.py                # 全量
    uv run --python 3.12 python e2e/api_flow.py --phase quick  # 只跑 P0-P3 + 一个作业
    uv run --python 3.12 python e2e/api_flow.py --keep         # 保留创建的作业便于人工排查

退出码 0 = 全部断言通过；1 = 有 FAIL（明细见 e2e/artifacts/ledger_api_flow.json）。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from client import (  # noqa: E402
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    API_BASE,
    API_DIRECT,
    TERMINAL_JOB_STATUSES,
    ApiClient,
    ApiError,
    Ledger,
    load_fixtures,
    load_ruleset_payload,
)

E2E_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = E2E_DIR / "artifacts"

# 数据源根固定为 NAS 挂载点：每个用例的 source_path 指向 /nas/<case>，
# 这样一个数据源就能覆盖全部目录形态（契约 §6.1：source_path 必须在根内且是目录）。
NAS_ROOT = "/nas"

# 每次运行的唯一后缀。Idempotency-Key 与作业一起持久化在 PG 里，跑第二遍必须换新 key，
# 否则会命中契约里正确的"同 key 异体 → 409"分支（规则集每次自增新版本，请求体必然不同）。
RUN_ID = str(time.time_ns())


# ============================================================================
# 工具
# ============================================================================
def detail_text(err: ApiError) -> str:
    """把 detail 归一成字符串：业务错误是字符串，框架校验错误是数组。"""
    body = err.body if isinstance(err.body, dict) else {}
    raw = body.get("detail")
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, ensure_ascii=False)


def expect_error(
    ledger: Ledger,
    name: str,
    call: Any,
    status: int,
    *,
    contains: str | None = None,
) -> bool:
    """负面用例统一入口：断言状态码，可选断言 detail 逐字包含某串。"""
    try:
        result = call()
    except ApiError as exc:
        ok = exc.status == status
        text = detail_text(exc)
        if ok and contains is not None:
            ok = contains in text
        ledger.check(name, ok, f"HTTP {exc.status} detail={text!r} (want {status} contains={contains!r})")
        return ok
    except Exception as exc:  # noqa: BLE001
        ledger.check(name, False, f"未抛 ApiError，而是 {type(exc).__name__}: {exc}")
        return False
    ledger.check(name, False, f"期望 HTTP {status}，实际成功返回 {str(result)[:160]}")
    return False


def create_job(
    api: ApiClient,
    *,
    name: str,
    data_source_id: int,
    source_path: str,
    rule_set_id: int,
    idem: str,
    file_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /api/jobs（带 Idempotency-Key），返回 JobRead。"""
    payload: dict[str, Any] = {
        "name": name,
        "data_source_id": data_source_id,
        "source_path": source_path,
        "rule_set_id": rule_set_id,
    }
    if file_filter is not None:
        payload["file_filter"] = file_filter
    import urllib.request

    url = f"{api.base}/jobs"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api.token}",
            "Idempotency-Key": idem,
        },
    )
    import urllib.error

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, json.loads(exc.read().decode("utf-8") or "{}"), "POST", "/jobs") from exc


def iter_matches(api: ApiClient, *, page: int = 500, **filters: Any) -> Any:
    """生成器：按契约 §7 的游标（created_before + id_before 成对）翻页拉全量命中。

    /api/matches 没有 offset 参数，传了也会被 FastAPI 忽略——只能用游标，
    且游标值就是上一页最后一行的 (created_at, id)。
    """
    params: dict[str, Any] = {**filters, "limit": page}
    while True:
        query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        batch = api.get(f"/matches?{query}")
        if not batch:
            return
        yield from batch
        if len(batch) < page:
            return
        last = batch[-1]
        params = {**{k: v for k, v in filters.items() if k != "created_before"},
                  "limit": page, "created_before": last["created_at"], "id_before": last["id"]}


def matches_for_rule(api: ApiClient, job_id: int, rule_id: str) -> list[dict[str, Any]]:
    return list(iter_matches(api, job_id=job_id, rule_id=rule_id))


def list_all(api: ApiClient, path: str, *, page: int = 500, key: str = "offset") -> list[dict[str, Any]]:
    """jobs/files/workers 这类 limit+offset 列表拉全量。"""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = api.get(f"{path}{'&' if '?' in path else '?'}limit={page}&{key}={offset}")
        if not batch:
            return rows
        rows.extend(batch)
        if len(batch) < page:
            return rows
        offset += page


# ============================================================================
# P0 健康与匿名访问
# ============================================================================
def phase_health(ledger: Ledger, api: ApiClient) -> None:
    ledger.phase("P0 健康检查与匿名访问")
    health = api.get("/health", expect_status=200)
    ledger.equal("GET /api/health 载荷", health, {"status": "ok", "service": "logharvest"})
    ready = api.get("/health/ready", expect_status=200)
    ledger.equal("GET /api/health/ready（PG 连通）", ready, {"status": "ready", "service": "logharvest"})
    expect_error(ledger, "未登录访问 /api/jobs → 401", lambda: api.get("/jobs"), 401)
    anon = ApiClient(base=API_BASE, token=None)
    ledger.check(
        "匿名 /api/health 不需要 token",
        anon.get("/health", expect_status=200)["status"] == "ok",
    )


# ============================================================================
# P1 认证
# ============================================================================
def phase_auth(ledger: Ledger, api: ApiClient) -> dict[str, Any]:
    ledger.phase("P1 认证与令牌")
    login = api.post("/login", {"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}, expect_status=200)
    data = login["data"]
    ledger.equal("登录信封 code/message", (login["code"], login["message"]), (0, "ok"))
    ledger.check("登录返回 accessToken/refreshToken", bool(data.get("accessToken")) and bool(data.get("refreshToken")))
    ledger.equal("admin 权限码含通配", data["permissions"], ["*:*:*"])
    ledger.equal("admin 角色", data["roles"], ["admin"])
    ledger.check("expires 是毫秒 epoch", int(data["expires"]) > 1_700_000_000_000, str(data["expires"]))
    api.token = data["accessToken"]

    routes = api.get("/auth/routes", expect_status=200)
    tree = routes["data"] if isinstance(routes, dict) and "data" in routes else routes
    ledger.check("auth/routes 信封且非空", isinstance(tree, list) and len(tree) > 0, f"顶层菜单数={len(tree) if isinstance(tree, list) else 'N/A'}")

    fresh = api.refresh(data["refreshToken"])
    # 别把这条断言写成"新令牌字符串必须不同"：JWT 载荷只有 {sub, username, type, exp}
    # （src/domain/services/tokens.py，无 jti/iat），exp 截到秒，同一秒内对同一主体签发的
    # 令牌逐字节相同。那是 HMAC 的确定性，不是缺陷。真正要保证的是"换出来的令牌能用"
    # 且"过期时间右移"，外加 refresh/access 的 type 隔离（refresh 当 bearer 必须 401）。
    probe = ApiClient(base=API_BASE, token=fresh["accessToken"])
    probe.get("/auth/routes", expect_status=200)
    ledger.check(
        "refresh 返回的 accessToken 可用",
        bool(fresh.get("accessToken")) and bool(fresh.get("refreshToken")),
        f"expires={fresh.get('expires')}",
    )
    ledger.check(
        "refresh 后 expires 不早于登录时",
        int(fresh["expires"]) >= int(data["expires"]) - 1000,
        f"fresh={fresh['expires']} login={data['expires']}",
    )
    expect_error(
        ledger, "refreshToken 不能当 accessToken 用（type 隔离）",
        lambda: ApiClient(base=API_BASE, token=data["refreshToken"]).get("/jobs"), 401,
    )

    time.sleep(1.05)  # 跨过一秒，才能观察到同主体令牌确实随 exp 右移而换新
    second = api.refresh(fresh["refreshToken"])
    ledger.check("连续 refresh 会随时间签发新令牌", second["accessToken"] != fresh["accessToken"], f"exp={second['expires']}")

    bad = ApiClient(base=API_BASE)
    expect_error(ledger, "错密码 → 401 invalid credentials", lambda: bad.post("/login", {"username": ADMIN_USERNAME, "password": "***"}), 401, contains="invalid credentials")
    expect_error(ledger, "登录体缺字段 → 422 数组 detail", lambda: bad.post("/login", {"username": ADMIN_USERNAME}), 422)
    expect_error(ledger, "伪造 token → 401", lambda: ApiClient(base=API_BASE, token="***").get("/jobs"), 401)
    expect_error(ledger, "refresh 传错字段名 → 422", lambda: bad.post("/refresh-token", {"refresh_token": "x"}), 422)

    logout_client = ApiClient(base=API_BASE, token=data["accessToken"])
    logout = logout_client.post("/logout", expect_status=200)
    ledger.check("logout 信封", isinstance(logout, dict) and logout.get("code") == 0, str(logout)[:120])
    return data


# ============================================================================
# P2 数据源与只读守卫
# ============================================================================
def phase_data_sources(ledger: Ledger, api: ApiClient) -> int:
    ledger.phase("P2 数据源与 NAS 只读守卫")

    def ensure(payload: dict[str, Any]) -> dict[str, Any]:
        """创建数据源；重跑时同名 409 就复用既有行（整套流程必须可反复执行）。"""
        try:
            return api.post("/data-sources", payload, expect_status=201)
        except ApiError as exc:
            if exc.status == 409 and "already exists" in detail_text(exc):
                rows = api.get("/data-sources", expect_status=200)
                existing = next((r for r in rows if r["name"] == payload["name"]), None)
                if existing:
                    ledger.check(f"复用既有数据源 {payload['name']}（重跑）", True, "409 → 按 name 找回")
                    return existing
            raise

    created = ensure(
        {
            "name": "e2e-nas",
            "root_path": NAS_ROOT,
            "read_only": True,
            "copy_to_local": True,
            "description": "E2E 只读 NAS（e2e/nas bind mount）",
        }
    )
    ds_id = created["id"]
    ledger.equal("数据源 type/enabled", (created["type"], created["enabled"]), ("mount", True))
    ledger.equal("数据源 root_path", created["root_path"], NAS_ROOT)

    test = api.post(f"/data-sources/{ds_id}/test", expect_status=200)
    ledger.equal("目录探测 exists/is_dir/readable", (test["exists"], test["is_dir"], test["readable"]), (True, True, True))
    # 契约 §4：virtiofs bind mount :ro 时 /proc/self/mounts 命中 ro → read_only。
    # 若这里是 unknown/writable，说明挂载方式退化了，enforce 策略会连锁失败。
    ledger.equal("writability 判定（enforce 前提）", test["writability"], "read_only")
    ledger.equal("read_only_confirmed", test["read_only_confirmed"], True)

    expect_error(
        ledger, "read_only=false → 422（Literal[True]）",
        lambda: api.post("/data-sources", {"name": "e2e-rw", "root_path": NAS_ROOT, "read_only": False}),
        422,
    )
    expect_error(
        ledger, "重名 → 409",
        lambda: api.post("/data-sources", {"name": "e2e-nas", "root_path": NAS_ROOT}),
        409, contains="data source name already exists",
    )
    expect_error(ledger, "不存在的 id 测试 → 404", lambda: api.post("/data-sources/999999/test"), 404, contains="data source not found")

    # 可写目录的判定链：server 容器里的 /local-tmp 是可写卷，root_path 指过去应判 writable。
    writable_ds = ensure({"name": "e2e-writable", "root_path": "/local-tmp", "read_only": True, "copy_to_local": False})
    verdict = api.post(f"/data-sources/{writable_ds['id']}/test", expect_status=200)
    ledger.check(
        "可写卷判定不为 read_only",
        verdict["writability"] != "read_only" and verdict["read_only_confirmed"] is False,
        f"writability={verdict['writability']}",
    )
    ledger.writable_ds_id = writable_ds["id"]  # type: ignore[attr-defined]
    return ds_id


# ============================================================================
# P3 规则集
# ============================================================================
def phase_rule_sets(ledger: Ledger, api: ApiClient) -> dict[str, int]:
    ledger.phase("P3 规则集与校验")
    # artifacts 里存的是裸 RuleSetConfig（make_nas.py 直接落盘），提交时套上信封。
    canonical_config = load_ruleset_payload("canonical_ruleset.json")
    canonical = {"name": canonical_config["name"], "config": canonical_config}
    created = api.post("/rule-sets", canonical, expect_status=201)
    ids = {"canonical": created["id"]}
    ledger.check("canonical 首版 version 是正整数", created["version"] >= 1, str(created["version"]))

    # 同名再提交 → 服务端 max+1 自增（不接受客户端传 version）。
    # 用相对断言：整套流程要能对着同一个数据卷反复跑，硬编码 1/2 只有首跑成立。
    again = api.post("/rule-sets", canonical, expect_status=201)
    ids["canonical_v2"] = again["id"]
    ledger.equal("同名重复提交自增版本", again["version"], created["version"] + 1)
    ledger.check("版本主键独立", again["id"] != created["id"])

    detail = api.get(f"/rule-sets/{ids['canonical']}", expect_status=200)
    ledger.equal("created_by 是令牌用户名", detail["created_by"], ADMIN_USERNAME)
    ledger.check("config 回读一致", detail["config"]["rules"][0]["id"] == canonical_config["rules"][0]["id"])

    # 契约 §5：body 里带 version 时服务端要么 422 拒绝、要么忽略后自增，绝不能用客户端值。
    # 期望写成"上一步的 version + 1"，因为同名规则集每次重跑都会自增，硬编码 3 只在首跑成立。
    with_version = api.call("POST", "/rule-sets", {**canonical, "version": 99})
    if with_version["status"] == 201:
        ledger.check(
            "客户端 version=99 不生效（被忽略并自增）",
            with_version["body"]["version"] == again["version"] + 1,
            f"实际 version={with_version['body']['version']} 期望 {again['version'] + 1}",
        )
        ids["canonical_v3"] = with_version["body"]["id"]
    else:
        ledger.check(
            "客户端 version=99 被直接拒绝（422）",
            with_version["status"] == 422,
            f"HTTP {with_version['status']}",
        )

    # 规则语义负面用例（契约 §5：config 非法 → 422 字符串 detail）
    def bad_rule(name: str, config: dict[str, Any], text: str) -> None:
        # 规则集名取自用例名：多个用例的 detail 前缀相同（都以 "invalid " 开头）时，
        # 用 text[:8] 会让它们共用一个名字、彼此抬版本，排查时对不上号。
        slug = "".join(ch if ch.isalnum() else "-" for ch in name)[:28].strip("-")
        expect_error(
            ledger, name,
            lambda: api.post("/rule-sets", {"name": f"e2e-bad-{slug}", "config": config}),
            422, contains=text,
        )

    base_rule = canonical_config["rules"][0]
    bad_rule(
        "keyword 配 threshold → 422",
        {"name": "k", "rules": [{**base_rule, "match": {"type": "keyword", "any": ["x"]}, "threshold": {"field": "ms", "op": "gt", "value": 1}}]},
        "threshold requires a regex match",
    )
    bad_rule(
        "any/all 同时给 → 422",
        {"name": "k", "rules": [{"id": "r", "match": {"type": "keyword", "any": ["x"], "all": ["y"]}}]},
        "exactly one of any/all",
    )
    bad_rule(
        "重复 rule id → 422",
        {"name": "k", "rules": [dict(base_rule), dict(base_rule)]},
        "rule ids must be unique",
    )
    bad_rule(
        "非法正则 → 422",
        {"name": "k", "rules": [{"id": "r", "match": {"type": "regex", "pattern": "("}}]},
        # 服务端把 re 的原话包在自己的文案里：
        # "rule 'r': invalid regex pattern: missing ) at position 1"。
        # 断言只锁自己这层前缀，不去匹配 re 内部随版本变的具体报错。
        "invalid regex pattern",
    )
    bad_rule(
        "flags 越界 → 422",
        {"name": "k", "rules": [{"id": "r", "match": {"type": "regex", "pattern": "x", "flags": "xu"}}]},
        "flags",
    )
    bad_rule(
        "threshold 字段非命名组 → 422",
        {"name": "k", "rules": [{"id": "r", "match": {"type": "regex", "pattern": "x(?<g>\\d+)"}, "threshold": {"field": "nope", "op": "gt", "value": 1}}]},
        "nope",
    )
    # gitwildmatch 允许未闭合的字符类（"[" 合法），真正非法的是尾随反斜杠转义。
    # 这里用 pathspec 实际会抛 GitIgnorePatternError 的模式，与单元测试 invalid-glob 一致。
    bad_rule(
        "非法 file_glob → 422",
        {"name": "k", "file_glob": "logs\\", "rules": [dict(base_rule)]},
        "invalid file_glob",
    )
    # 反向确认：gitwildmatch 里未闭合字符类是**合法**模式，服务端不应过度校验把它挡掉，
    # 否则真实规则集里合法的 glob 会被误拒（201 而不是 422）。
    odd_glob = api.post(
        "/rule-sets",
        {"name": "e2e-odd-glob", "config": {"name": "k", "file_glob": "[", "rules": [dict(base_rule)]}},
        expect_status=201,
    )
    ledger.check("未闭合字符类 '[' 不被误拒（201）", odd_glob["id"] > 0, f"id={odd_glob['id']}")
    bad_rule(
        "未知编码 → 422",
        {"name": "k", "encoding": "no-such-codec", "rules": [dict(base_rule)]},
        "unknown encoding",
    )

    # 作业会用到的专用规则集：file_glob 收窄 + 5k 规则性能集
    glob_cfg = dict(canonical_config)
    glob_cfg["file_glob"] = "nested/*"
    ids["glob"] = api.post("/rule-sets", {"name": "e2e-glob-nested", "config": glob_cfg}, expect_status=201)["id"]

    perf_config = load_ruleset_payload("perf_ruleset.json")
    perf = api.post(
        "/rule-sets", {"name": perf_config["name"], "config": perf_config}, expect_status=201
    )
    ids["perf"] = perf["id"]
    ledger.check(
        "5k 规则集入库",
        perf["id"] > 0 and len(perf_config["rules"]) == 5001,
        f"rule_set_id={perf['id']} rules={len(perf_config['rules'])}",
    )

    listed = api.get("/rule-sets", expect_status=200)
    ledger.check("rule-sets 列表按 name,version 升序", len(listed) >= 4 and {"id", "name", "version", "created_at"} <= set(listed[0]), f"count={len(listed)}")
    return ids


# ============================================================================
# P4 作业契约与幂等（全部是 API 侧断言，不依赖 Worker 进度）
# ============================================================================
def jobs_request(api: ApiClient, payload: dict[str, Any], *, idem: str | None = None) -> dict[str, Any]:
    """裸 POST /api/jobs：返回 {status, body}，负面用例用。idem=None 表示不发这个头。"""
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api.token}"}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    request = urllib.request.Request(
        f"{api.base}/jobs", data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return {"status": response.status, "body": json.loads(response.read().decode("utf-8") or "{}")}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            body = {"detail": raw}
        return {"status": exc.code, "body": body}


def detail_of(resp: dict[str, Any]) -> str:
    raw = resp["body"].get("detail") if isinstance(resp["body"], dict) else None
    return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)


def check_error(ledger: Ledger, name: str, resp: dict[str, Any], status: int, *, contains: str | None = None) -> bool:
    ok = resp["status"] == status
    text = detail_of(resp)
    if ok and contains is not None:
        ok = contains in text
    ledger.check(name, ok, f"HTTP {resp['status']} detail={text[:220]!r}（期望 {status} contains={contains!r}）")
    return ok


def phase_job_contract(ledger: Ledger, api: ApiClient, ds_id: int, ids: dict[str, int]) -> None:
    ledger.phase("P4 作业契约与幂等")

    def payload(**over: Any) -> dict[str, Any]:
        base = {"name": "e2e-contract", "data_source_id": ds_id, "source_path": "/nas/clean", "rule_set_id": ids["canonical"]}
        base.update(over)
        return base

    # --- 幂等四态：201 首建 / 200 同体重放 / 409 异体重用 / 400 空白 key ---
    # key 必须每次运行唯一：作业与 Idempotency-Key 都持久在 PG 里，跑第二遍时
    # 规则集是自增新版本（rule_set_id 变了），沿用固定 key 会命中"同 key 异体"分支
    # 拿到 409 —— 那是契约的正确行为，不是缺陷。跨运行的 key 复用另见 §6.1 的手工验证。
    key = f"e2e-idem-{RUN_ID}"
    first = jobs_request(api, payload(), idem=key)
    check_error(ledger, "带 Idempotency-Key 首建 → 201", {"status": first["status"], "body": first["body"]}, 201)
    ledger.equal("201 的响应 status 字段", first["status"], 201)
    job_id = first["body"].get("id")
    replay = jobs_request(api, payload(), idem=key)
    ledger.equal("同 key 同体 → HTTP 200（重放）", replay["status"], 200)
    ledger.equal("重放返回同一作业 id", replay["body"].get("id"), job_id)
    conflict = jobs_request(api, payload(name="e2e-contract-other"), idem=key)
    check_error(ledger, "同 key 异体 → 409", conflict, 409, contains="Idempotency-Key was already used with a different request body")
    blank = jobs_request(api, payload(), idem="   ")
    check_error(ledger, "空白 key → 400", blank, 400, contains="Idempotency-Key must not be blank")
    # key 只做 strip()：带前后空格的同一个 key 仍应命中重放分支
    padded = jobs_request(api, payload(), idem=f"  {key}  ")
    ledger.equal("key 两端空格被 strip（仍重放）", (padded["status"], padded["body"].get("id")), (200, job_id))
    nokey_a = jobs_request(api, payload(name="e2e-nokey"))
    nokey_b = jobs_request(api, payload(name="e2e-nokey"))
    ledger.equal("不带 key 两次都是 201", (nokey_a["status"], nokey_b["status"]), (201, 201))
    ledger.check("不带 key 每次新建作业", nokey_a["body"]["id"] != nokey_b["body"]["id"])

    # --- 引用校验 ---
    check_error(ledger, "未知数据源 → 404", jobs_request(api, payload(data_source_id=999999)), 404, contains="data source not found or disabled")
    check_error(ledger, "未知规则集 → 404", jobs_request(api, payload(rule_set_id=999999)), 404, contains="rule set not found")
    check_error(ledger, "source_path 越出根 → 400", jobs_request(api, payload(source_path="/etc")), 400, contains="source_path is outside data source root")
    check_error(ledger, "source_path 不是目录 → 400", jobs_request(api, payload(source_path="/nas/clean/app.zip")), 400, contains="source_path is not a directory")
    check_error(
        ledger, "可写挂载 + enforce → 409",
        jobs_request(api, payload(data_source_id=ledger.writable_ds_id, source_path="/local-tmp")),
        409, contains="is mounted writable; refusing to process",
    )
    check_error(
        ledger, "suffixes 与压缩后缀交集为空 → 400",
        jobs_request(api, payload(file_filter={"suffixes": [".txt", ".log"]})),
        400, contains="must contain at least one compressed archive suffix",
    )
    ledger.check(
        "suffixes 含合法压缩后缀可创建",
        jobs_request(api, payload(name="e2e-suffix", file_filter={"suffixes": [".zip"]}))[
            "status"
        ]
        in (200, 201),
    )

    # --- 参数边界（FastAPI 框架 422，detail 是数组）---
    for name, over in [
        ("max_depth=0 → 422", {"file_filter": {"max_depth": 0}}),
        ("max_depth=21 → 422", {"file_filter": {"max_depth": 21}}),
        ("max_files=0 → 422", {"file_filter": {"max_files": 0}}),
        ("max_files=20001 → 422", {"file_filter": {"max_files": 20001}}),
        ("name 空串 → 422", {"name": ""}),
        ("data_source_id 非整数 → 422", {"data_source_id": "abc"}),
    ]:
        resp = jobs_request(api, {**payload(), **over})
        check_error(ledger, name, resp, 422)
        if resp["status"] == 422:
            ledger.check(
                f"{name} 的 detail 是数组（框架校验形状）",
                isinstance(resp["body"].get("detail"), list),
                str(resp["body"].get("detail"))[:160],
            )

    # --- 0 文件目录：创建即终态 ---
    empty_job = api.post("/jobs", payload(name="e2e-empty-dir", source_path="/nas/empty"), expect_status=201)
    ledger.equal("0 文件作业直接 succeeded", empty_job["status"], "succeeded")
    ledger.check("0 文件作业 finished_at 已写", bool(empty_job["finished_at"]), str(empty_job["finished_at"]))
    ledger.equal("0 文件作业 total_files", empty_job["total_files"], 0)
    ledger.equal("0 文件作业没有 error", empty_job["error"], None)

    # --- 读接口 ---
    expect_error(ledger, "GET /jobs/999999 → 404", lambda: api.get("/jobs/999999"), 404, contains="job not found")
    expect_error(ledger, "GET /jobs?status=bogus → 422", lambda: api.get("/jobs?status=bogus"), 422)
    check_error(ledger, "未知作业的 files → 404", api.call("GET", "/jobs/999999/files"), 404, contains="job not found")
    files_of_empty = api.get(f"/jobs/{empty_job['id']}/files", expect_status=200)
    ledger.equal("0 文件作业的 files 是空数组", files_of_empty, [])
    sse_missing = api.call("GET", "/jobs/999999/events")
    check_error(ledger, "未知作业的 SSE → 404", sse_missing, 404, contains="job not found")
    ledger.created_contract_job = job_id  # type: ignore[attr-defined]


# ============================================================================
# P5 抓取 / 提取矩阵：每个夹具用例跑一个作业，与独立数值 oracle 逐字段对账
# ============================================================================
def job_report(api: ApiClient, job_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    job = api.get(f"/jobs/{job_id}", expect_status=200)
    tasks = list_all(api, f"/jobs/{job_id}/files")
    return job, tasks


def expect_value(ledger: Ledger, name: str, actual: Any, expected: Any) -> bool:
    """oracle 里 None 表示"这个值夹具不预测"（例如截断流能解出多少行），
    必须显式 SKIP 而不是拿 None 去比 —— 静默比不过会变成假失败。
    """
    if expected is None:
        ledger.skip(name, "oracle 不预测该值（见 fixtures 备注）")
        return True
    return ledger.equal(name, actual, expected)


def run_case(
    ledger: Ledger,
    api: ApiClient,
    *,
    case_name: str,
    spec: dict[str, Any],
    ds_id: int,
    rule_set_id: int,
    file_filter: dict[str, Any] | None = None,
    check_matches_by_rule: bool = True,
    timeout: float = 900.0,
) -> dict[str, Any]:
    """跑一个用例并把结果与 fixtures.json 的 oracle 对账，返回 job/task 快照。"""
    payload = {
        "name": f"e2e-{case_name}",
        "data_source_id": ds_id,
        "source_path": spec["source"],
        "rule_set_id": rule_set_id,
    }
    if file_filter is not None:
        payload["file_filter"] = file_filter
    job = api.post("/jobs", payload, expect_status=201)
    tag = f"[{case_name}]"
    try:
        job = api.wait_job_terminal(job["id"], timeout=timeout)
    except TimeoutError as exc:
        ledger.check(f"{tag} 作业进入终态", False, str(exc))
        return {"job": job, "tasks": []}
    tasks = list_all(api, f"/jobs/{job['id']}/files")
    by_name = {t["file_name"]: t for t in tasks}

    totals_match = all(
        [
            ledger.equal(f"{tag} total_files", job["total_files"], spec["expect_total_files"]),
            ledger.equal(f"{tag} 文件任务行数", len(tasks), spec["expect_total_files"]),
            expect_value(
                ledger, f"{tag} 处理总行数", sum(t["processed_lines"] or 0 for t in tasks), spec["expect_total_lines"]
            ),
            ledger.equal(
                f"{tag} 命中总行数", sum(t["matched_rows"] or 0 for t in tasks), spec["expect_total_matches"]
            ),
        ]
    )
    # 计数一偏差，FAIL 行只有 "actual=0 expected=37" 这种数字，排查要从文件任务一路翻到
    # 容器里才知道是临时盘属主、预算越界还是源文件漂移。这里主动补一行归因。
    # 判据用"oracle 对不上"而不是"状态不是 succeeded"：partial/truncated 这类用例的
    # 非成功终态本来就是预期，拿状态当信号会自己造出一批假失败。
    if not totals_match:
        ledger.skip(f"{tag} 偏差归因（文件任务 error 摘要）", job_failure_hint(api, job))

    for file_name, expected in (spec.get("file_statuses") or {}).items():
        task = by_name.get(file_name)
        if task is None:
            ledger.check(f"{tag} {file_name} 出现在文件任务里", False, "missing")
            continue
        details = spec["per_file"][file_name]
        if expected == "unknown":
            ledger.check(
                f"{tag} {file_name} 非 succeeded 且 error 非空",
                task["status"] != "succeeded" and bool(task["error"]),
                f"status={task['status']} error={str(task['error'])[:160]}",
            )
        else:
            ledger.equal(f"{tag} {file_name} 终态", task["status"], expected)
        expect_value(ledger, f"{tag} {file_name} 行数", task["processed_lines"], details["lines"])
        ledger.equal(f"{tag} {file_name} 命中数", task["matched_rows"], details["matches"])
        ledger.check(
            f"{tag} {file_name} 有 worker_id/attempts/heartbeat",
            bool(task["worker_id"]) and task["attempts"] >= 1 and bool(task["heartbeat_at"]),
            f"worker={task['worker_id']} attempts={task['attempts']}",
        )

    if check_matches_by_rule:
        for rule_id, expected in (spec.get("expect_matches_by_rule") or {}).items():
            got = len(list(iter_matches(api, page=500, job_id=job["id"], rule_id=rule_id)))
            ledger.equal(f"{tag} 规则 {rule_id} 命中数", got, expected)

    # 任务计数器与命中表必须自洽：matched_rows 报 N，/matches 就得数出 N 行。
    # 截断/损坏用例的 oracle 是 0，这条同时钉住"任务没跑完就不许发布半截命中"。
    # flood/perf 命中上千，翻页拉全量不划算，只在 2000 行以内做交叉核对。
    published = sum(t["matched_rows"] or 0 for t in tasks)
    if published <= 2000:
        rows = len(list(iter_matches(api, page=500, job_id=job["id"])))
        ledger.equal(f"{tag} 任务命中计数 = matches 表行数", rows, published)
    else:
        ledger.skip(f"{tag} 任务命中计数 = matches 表行数", f"命中 {published} 行，翻页拉全量不划算")

    attempts = api.get(f"/jobs/{job['id']}/files/{tasks[0]['id']}/attempts", expect_status=200) if tasks else []
    if tasks:
        ledger.check(
            f"{tag} 首个任务的 attempt 历史",
            len(attempts) >= 1 and attempts[0]["attempt_no"] == 1 and bool(attempts[0]["worker_version"]),
            json.dumps(attempts[:1], ensure_ascii=False)[:200],
        )
    ledger.check(f"{tag} 作业终态合法", job["status"] in TERMINAL_JOB_STATUSES, job["status"])
    return {"job": job, "tasks": tasks, "by_name": by_name, "payload": payload, "id": job["id"]}


def phase_harvest(ledger: Ledger, api: ApiClient, ds_id: int, ids: dict[str, int]) -> dict[str, Any]:
    ledger.phase("P5 抓取与文件提取矩阵（与夹具 oracle 对账）")
    fixtures = load_fixtures()["cases"]
    results: dict[str, Any] = {}
    # deep 用例在默认 max_depth=5 下会裁掉 7 层目录里的 d7.zip（oracle: pruned），
    # 同一份 NAS 用 max_depth=20 再跑一次必须多 1 个文件、6 行、5 个命中。
    deep_full_spec = {
        **fixtures["deep"],
        "expect_total_files": fixtures["deep"]["deep_full"]["expect_total_files"],
        "expect_total_lines": fixtures["deep"]["deep_full"]["expect_total_lines"],
        "expect_total_matches": fixtures["deep"]["deep_full"]["expect_total_matches"],
        "file_statuses": {},
        "per_file": {},
    }
    # 覆盖：多格式、嵌套成员、部分损坏、截断流、空目录、杂项忽略、深度阈值、中文编码、
    # 穿越/软链、规模与预算、命中洪水、5k 规则性能。modified/cancel 归 P10，预算越界归 limits_flow。
    plan = [
        ("clean", "canonical", None),
        ("formats", "canonical", None),
        ("partial", "canonical", None),
        ("truncated", "canonical", None),
        ("empty", "canonical", None),
        ("misc", "canonical", None),
        ("deep", "canonical", None),
        ("deep@max_depth=20", "canonical", None),
        ("encoding", "canonical", None),
        ("traversal", "canonical", None),
        ("limits", "canonical", None),
        ("flood", "canonical", None),
        ("perf", "perf", None),
    ]
    for case_name, ruleset_key, _ in plan:
        real = "deep" if case_name.startswith("deep@") else case_name
        spec = deep_full_spec if case_name.startswith("deep@") else fixtures.get(real)
        if spec is None:
            ledger.skip(f"{case_name} 用例", "fixtures.json 里没有该用例")
            continue
        results[case_name] = run_case(
            ledger,
            api,
            case_name=case_name,
            spec=spec,
            ds_id=ds_id,
            rule_set_id=ids[ruleset_key],
            file_filter=fixtures["deep"]["deep_full"]["file_filter"] if case_name.startswith("deep@") else None,
            check_matches_by_rule=case_name not in ("perf", "flood", "limits", "deep@max_depth=20"),
        )
        if case_name == "deep":
            names = set(results[case_name].get("by_name") or {})
            ledger.check(
                "[deep] 默认 max_depth=5 裁掉 7 层目录里的 d7.zip",
                "d7.zip" not in names and len(names) == 5,
                f"names={sorted(names)}",
            )
        if case_name.startswith("deep@"):
            names = set(results[case_name].get("by_name") or {})
            task = (results[case_name].get("by_name") or {}).get("d7.zip")
            ledger.check("[deep@20] d7.zip 被纳入", "d7.zip" in names and len(names) == 6, f"names={sorted(names)}")
            if task:
                ledger.equal(
                    "[deep@20] d7.zip 行数/命中数",
                    (task["processed_lines"], task["matched_rows"]),
                    (fixtures["deep"]["deep_full"]["extra_lines"], fixtures["deep"]["deep_full"]["extra_matches"]),
                )

    # 空目录用例的 oracle 是 0 文件；run_case 里 total_files==0 已断言，这里补状态。
    if results.get("empty", {}).get("job"):
        ledger.equal("[empty] 0 文件作业终态 succeeded", results["empty"]["job"]["status"], "succeeded")

    # 越界成员必须没有被解到 NAS 上（宿主机侧直接看文件系统）。
    nas_host = Path(__file__).resolve().parent / "nas"
    for rel in fixtures["traversal"].get("must_not_exist") or []:
        host_path = nas_host / rel.replace("/nas/", "")
        ledger.check(f"[traversal] {rel} 未被写出（宿主侧不存在）", not host_path.exists(), str(host_path))

    # 越界成员名的处理口径是"流式读取、从不解包"：`../escape.log` 对 Worker 只是一串
    # 字符，既不参与路径拼接也不落盘，所以它的内容算作普通数据、member_path 原样保留。
    # 真正的风险是符号链接成员——必须整条跳过。跟链读会把 escape 的 5 行再数一遍，
    # 行数立刻从 20 涨到 25，所以"行数对得上"本身就是符号链接未被跟随的证据。
    trv = results.get("traversal", {})
    if trv.get("job"):
        trv_rows = list(iter_matches(api, page=500, job_id=trv["job"]["id"]))
        ledger.check(
            "[traversal] 越界成员名原样进 member_path（未改名、未落盘）",
            {"../escape.log", "sub/../../escape2.log", "../t.escape.log"}
            <= {r["member_path"] for r in trv_rows},
            f"members={sorted({r['member_path'] for r in trv_rows})}",
        )
        ledger.check(
            "[traversal] 符号链接成员未产出命中（未跟随）",
            not ({r["file_name"] for r in trv_rows} & {"link.log", "tlink.log"}),
            f"file_names={sorted({r['file_name'] for r in trv_rows})}",
        )

    # file_glob 收窄（规则集级，非 file_filter）：/nas/clean 用 e2e-glob-nested 跑，
    # 命中必须全部落在 nested/ 下，且命中总数小于不启用 glob 的 clean 用例。
    glob_job = api.post(
        "/jobs",
        {
            "name": "e2e-clean-glob",
            "data_source_id": ds_id,
            "source_path": fixtures["clean"]["source"],
            "rule_set_id": ids["glob"],
        },
        expect_status=201,
    )
    glob_job = api.wait_job_terminal(glob_job["id"])
    hits = list(iter_matches(api, page=500, job_id=glob_job["id"]))
    ledger.check(
        "[glob] 成员级 file_glob 后仍有命中",
        len(hits) > 0,
        f"hits={len(hits)}",
    )
    ledger.check(
        "[glob] 命中全部落在 nested/ 下",
        all(h["member_path"].startswith(fixtures["clean"]["glob_case"]["expect_members_prefix"]) for h in hits),
        f"违例={[h['member_path'] for h in hits if not h['member_path'].startswith('nested/')][:3]}",
    )
    ledger.check(
        "[glob] 命中数少于不过滤的 clean 用例",
        len(hits) < fixtures["clean"]["expect_total_matches"],
        f"glob={len(hits)} clean={fixtures['clean']['expect_total_matches']}",
    )
    ledger.harvest = results  # type: ignore[attr-defined]
    return results


# ============================================================================
# P6 命中查询契约 + SSE 进度流
# ============================================================================
def open_sse(api: ApiClient, path: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """读完整 SSE 流，返回（响应头, 解析出的 progress 帧）。契约 §6.5 的报文头只能这样验。"""
    import urllib.request

    request = urllib.request.Request(
        f"{api.base}{path}",
        headers={"Authorization": f"Bearer {api.token}", "Accept": "text/event-stream"},
    )
    frames: list[dict[str, Any]] = []
    with urllib.request.urlopen(request, timeout=180) as response:
        headers = {k.lower(): v for k, v in response.headers.items()}
        event: str | None = None
        data: list[str] = []
        for raw in response:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if data:
                    payload = json.loads("\n".join(data))
                    if event in (None, "progress"):
                        frames.append(payload)
                event, data = None, []
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data.append(line.split(":", 1)[1].strip())
    return headers, frames


def sse_headers(api: ApiClient, path: str) -> dict[str, str]:
    """只取 SSE 响应头就断开，不消费整条流。

    专用于断言"应用原始响应头"：nginx 把 `X-Accel-*` 当作给自己的加速指令消费，
    不会转发到客户端，所以这类头只能在直连 base 上验（业务口径仍走代理）。
    """
    import urllib.request

    request = urllib.request.Request(
        f"{api.base}{path}",
        headers={"Authorization": f"Bearer {api.token}", "Accept": "text/event-stream"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return {k.lower(): v for k, v in response.headers.items()}


def job_failure_hint(api: ApiClient, job: dict[str, Any]) -> str:
    """作业收尾不对时，把文件任务里出现过的 error 去重压成一行。

    只看计数偏差等于没有证据：/local-tmp 属主错、预算越界、源文件被换……全都表现
    为"命中恒为 0"，而 /api/health 和心跳一切正常。归因信息进 ledger，一次运行就能
    定位到环境问题还是真缺陷。
    """
    try:
        tasks = list_all(api, f"/jobs/{job['id']}/files")
    except (ApiError, OSError):
        return f"job={job['id']} status={job['status']} error={job.get('error')}"
    errors = sorted({str(t.get("error")) for t in tasks if t.get("error")})
    head = f"job={job['id']} status={job['status']} job_error={job.get('error')}"
    if not errors:
        return f"{head} task_errors=none"
    return f"{head} task_errors[distinct]={errors[:3]}"


def phase_matches(ledger: Ledger, api: ApiClient, ds_id: int, ids: dict[str, int]) -> None:
    ledger.phase("P6 命中查询与 SSE 进度流")
    fixtures = load_fixtures()["cases"]
    job = api.post(
        "/jobs",
        {"name": "e2e-matches", "data_source_id": ds_id, "source_path": "/nas/clean", "rule_set_id": ids["canonical"]},
        expect_status=201,
    )
    job = api.wait_job_terminal(job["id"])

    # --- 参数契约（逐字 detail，业务字符串 vs 框架数组两种 422 形状）---
    expect_error(ledger, "无 scope → 422", lambda: api.get("/matches"), 422, contains="either job_id or both created_from and created_to are required")
    expect_error(ledger, "只有 created_from → 422", lambda: api.get("/matches?created_from=2026-01-01T00:00:00"), 422, contains="either job_id or both created_from and created_to are required")
    expect_error(
        ledger, "created_from 晚于 created_to → 422",
        lambda: api.get("/matches?created_from=2026-01-02T00:00:00&created_to=2026-01-01T00:00:00"),
        422, contains="created_from must not be after created_to",
    )
    expect_error(
        ledger, "游标不成对（只给 created_before）→ 422",
        lambda: api.get(f"/matches?job_id={job['id']}&created_before=2026-01-01T00:00:00"),
        422, contains="created_before and id_before must be provided together",
    )
    expect_error(
        ledger, "游标不成对（只给 id_before）→ 422",
        lambda: api.get(f"/matches?job_id={job['id']}&id_before=5"),
        422, contains="created_before and id_before must be provided together",
    )
    for name, qs in (("limit=501 → 422", "limit=501"), ("limit=0 → 422", "limit=0")):
        resp = api.call("GET", f"/matches?job_id={job['id']}&{qs}")
        check_error(ledger, name, resp, 422)
    # severity 是裸 `str | None`（routes_matches.py:22），不是 Literal：未知值不进 422，
    # 而是按等值过滤命中不到任何行 → 200 空数组。写这里是为了钉住"拼错严重度不会报错、
    # 只会静默返回空"这个行为，将来若加校验，这条断言会第一时间提醒改契约。
    sev = api.call("GET", f"/matches?job_id={job['id']}&severity=nope")
    ledger.equal("severity 未知值 → 200 空数组（等值过滤，不校验词表）", (sev["status"], sev["body"]), (200, []))
    ledger.check(
        "框架 422 的 detail 是数组",
        isinstance(api.call("GET", f"/matches?job_id={job['id']}&limit=501")["body"].get("detail"), list),
    )

    # --- 时间窗 scope 与 job_id scope 等价（闭区间边界）---
    rows_by_job = list(iter_matches(api, page=500, job_id=job["id"]))
    window = api.get(
        f"/matches?created_from=2000-01-01T00:00:00&created_to=2999-12-31T23:59:59&limit=500"
    )
    ledger.check("宽时间窗 scope 可用", len(window) >= len(rows_by_job), f"window={len(window)} job={len(rows_by_job)}")

    # --- 游标翻页：page=5 逐页拉，id 不重不漏且严格按 (created_at desc, id desc) ---
    seen: list[int] = []
    params: dict[str, Any] = {"job_id": job["id"], "limit": 5}
    pages = 0
    while True:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        batch = api.get(f"/matches?{qs}")
        pages += 1
        if not batch:
            break
        seen.extend(r["id"] for r in batch)
        if len(batch) < 5:
            break
        last = batch[-1]
        params = {"job_id": job["id"], "limit": 5, "created_before": last["created_at"], "id_before": last["id"]}
        if pages > 40:
            break
    ledger.equal("游标翻页拉到的总数 = 直拉总数", len(seen), len(rows_by_job))
    ledger.equal("游标翻页无重复 id", len(set(seen)), len(seen))
    ledger.check("游标翻页至少 3 页", pages >= 3, f"pages={pages}")
    ordered = sorted(rows_by_job, key=lambda r: (r["created_at"], r["id"]), reverse=True)
    ledger.equal("排序口径 created_at desc, id desc", [r["id"] for r in rows_by_job], [r["id"] for r in ordered])

    # --- 过滤维度 ---
    # /matches 的 `file_name` 是**压缩成员 basename**（`Path(info.filename).name`，
    # service/src/domain/archive/reader.py:293），归档全路径在 `source_path`。
    # 曾按 "app.zip" 过滤得到 0 行，那是字段口径拿错了，不是产品缺陷。
    clean_zip = fixtures["clean"]["per_file"]["app.zip"]
    for member_path, details in clean_zip["members"].items():
        basename = member_path.rsplit("/", 1)[-1]
        rows = list(iter_matches(api, page=500, job_id=job["id"], file_name=basename))
        ledger.equal(f"file_name={basename} 命中数（成员 basename 口径）", len(rows), details["matches"])
        ledger.check(
            f"file_name={basename} 结果同名单且 member_path 同成员",
            all(r["file_name"] == basename and r["member_path"] == member_path for r in rows),
            str([(r["file_name"], r["member_path"]) for r in rows[:2]]),
        )
    zip_rows = [r for r in rows_by_job if r["source_path"].endswith("/app.zip")]
    ledger.equal("source_path 才是归档路径（app.zip 命中数）", len(zip_rows), clean_zip["matches"])
    ledger.check(
        "归档名不出现在 file_name 里",
        all(r["file_name"] != "app.zip" for r in zip_rows),
        str(sorted({r["file_name"] for r in zip_rows})),
    )
    fuzzy = list(iter_matches(api, job_id=job["id"], file_name="app"))
    ledger.equal("file_name 不是模糊匹配", len(fuzzy), 0)
    rule_rows = matches_for_rule(api, job["id"], "error_kw")
    ledger.equal("rule_id 过滤命中数", len(rule_rows), fixtures["clean"]["expect_matches_by_rule"]["error_kw"])
    ledger.check("行号/成员/源文件字段齐备", all(r["line_no"] >= 1 and r["member_path"] and r["source_path"].startswith("/nas/") for r in rule_rows[:5]))
    ledger.check("MatchRead 不含 rule_set_id（契约 §7）", "rule_set_id" not in rule_rows[0], str(sorted(rule_rows[0]))[:200])
    sevs = {r["severity"] for r in rows_by_job}
    ledger.check("severity 只取词表值", sevs <= {"info", "warning", "error", "critical"}, str(sevs))
    slow = matches_for_rule(api, job["id"], "slow_regex")
    ledger.check("regex 命名组落进 fields", all(isinstance(r["fields"], dict) and r["fields"] for r in slow), str(slow[:1]))
    big = matches_for_rule(api, job["id"], "big_rows")
    ledger.check("threshold 规则有 numeric_value", all(r["numeric_value"] is not None for r in big), str(big[:1]))
    kw = matches_for_rule(api, job["id"], "error_kw")
    ledger.check("keyword 规则 fields 为空 dict", all(r["fields"] == {} for r in kw), str(kw[0]["fields"]))
    ledger.check("raw_truncated 默认 false", all(r["raw_truncated"] is False for r in rows_by_job))

    # --- SSE：报文头 + 帧载荷 + 终态收尾 ---
    headers, frames = open_sse(api, f"/jobs/{job['id']}/events?max_events=100")
    ledger.equal("SSE Content-Type（经 nginx）", headers.get("content-type", "").split(";")[0], "text/event-stream")
    ledger.equal("SSE Cache-Control（经 nginx）", headers.get("cache-control"), "no-cache")
    # `X-Accel-Buffering` 只能在直连应用的 base 上验：nginx 把 X-Accel-* 当作给自己的
    # 加速指令消费、不转发给客户端（实测 :18000 有该头、:18080 没有）。经代理这侧真正要
    # 证明的是"流没被缓冲"——下面能逐帧读到 progress 增量就是证据。
    direct = ApiClient(base=API_DIRECT, token=api.token)
    direct_headers = sse_headers(direct, f"/jobs/{job['id']}/events?max_events=1")
    ledger.equal("SSE X-Accel-Buffering（直连应用）", direct_headers.get("x-accel-buffering"), "no")
    ledger.equal(
        "SSE Content-Type（直连应用）", direct_headers.get("content-type", "").split(";")[0], "text/event-stream"
    )
    ledger.check(
        "经 nginx 后流式未被缓冲（多帧增量可见）",
        len(frames) >= 1 and frames[-1]["matched_rows"] >= 0,
        f"frames={len(frames)}",
    )
    ledger.check("SSE 至少一帧", len(frames) >= 1, f"frames={len(frames)}")
    if frames:
        first = frames[0]
        ledger.equal(
            "SSE 帧字段集合",
            sorted(first),
            sorted(["job_id", "status", "total", "by_status", "processed_lines", "matched_rows"]),
        )
        ledger.equal("SSE job_id 一致", {f["job_id"] for f in frames}, {job["id"]})
        ledger.check("SSE by_status 合计 = total", all(sum(f["by_status"].values()) == f["total"] for f in frames), str(frames[-1]))
        ledger.check("SSE 计数单调不回退", all(b["processed_lines"] >= a["processed_lines"] and b["matched_rows"] >= a["matched_rows"] for a, b in zip(frames, frames[1:])))
        ledger.equal("SSE 终帧状态 = 作业终态", frames[-1]["status"], job["status"])
        ledger.check("SSE 终帧是全量完成", frames[-1]["total"] == fixtures["clean"]["expect_total_files"], str(frames[-1]))
    expect_error(ledger, "SSE max_events=101 → 422", lambda: api.get(f"/jobs/{job['id']}/events?max_events=101"), 422)
    ledger.matches_job = job  # type: ignore[attr-defined]


# ============================================================================
# P7 数据上报：Worker 心跳/指标、系统状态、维护作业
# ============================================================================
def phase_reporting(ledger: Ledger, api: ApiClient) -> list[dict[str, Any]]:
    ledger.phase("P7 数据上报（Worker 心跳/指标与系统状态）")
    workers = api.get("/workers?limit=200&offset=0", expect_status=200)
    ledger.check("workers 列表非空", len(workers) >= 2, f"count={len(workers)}")
    ledger.equal("workers 按 id 升序", [w["id"] for w in workers], sorted(w["id"] for w in workers))
    ids = [w["id"] for w in workers]
    # 离线行是合法产品状态（limits_flow / U8 夹具故意留下的紧预算 Worker 行）：它不再
    # 心跳、resume 返回 409，下面的断言按"存活台"与"全部台"分开口径，P7 才能与
    # limits/UI 的运行顺序解耦。
    live_ids = [w["id"] for w in workers if w["status"] != "offline"]
    for w in workers:
        # 版本只对存活台收紧到 0.1.0：离线行可能是 limits_flow / U8 留下的外来夹具
        # （如 0.1.0-budget），它的版本反映的是"消失那一刻"的镜像，不该由 P7 裁决。
        is_live = w["status"] != "offline"
        ledger.check(
            f"worker {w['id']} 注册字段",
            (w["version"] == "0.1.0" or not is_live)
            and w["status"] in ("online", "busy", "draining", "offline") and bool(w["hostname"]) and w["max_concurrent_copies"] == 4,
            json.dumps({k: w[k] for k in ("version", "status", "hostname", "max_concurrent_copies")}, ensure_ascii=False),
        )
    # 指标：每轮 run_once 都会带上 psutil 采到的 cpu/rss/free（契约 §8.2，None 不覆盖旧值）。
    # 样本必须是存活台：离线夹具行从不上报，指标恒为 NULL。
    sample = next((w for w in workers if w["status"] != "offline"), workers[0])
    ledger.check(
        "worker 指标已上报（cpu/mem/disk 非空）",
        all(sample[k] is not None for k in ("cpu_percent", "memory_bytes", "temp_disk_free_bytes")),
        json.dumps({k: sample[k] for k in ("cpu_percent", "memory_bytes", "temp_disk_free_bytes")}),
    )
    ledger.check("memory_bytes 合理区间", 10_000_000 < (sample["memory_bytes"] or 0) < 5_000_000_000, str(sample["memory_bytes"]))
    # 注意别写成 `cpu_percent or -1`：空闲 Worker 的 psutil 首采样就是 0.0，而 0.0 是假值，
    # 会被 `or` 换成 -1，断言把完全正常的上报判成失败。
    cpu = sample["cpu_percent"]
    ledger.check("cpu_percent 是 0-100*核数 的均值", cpu is not None and 0.0 <= cpu < 3200.0, str(cpu))

    before = {w["id"]: w["last_heartbeat"] for w in workers}
    time.sleep(6.0)
    after = {w["id"]: w["last_heartbeat"] for w in api.get("/workers?limit=200", expect_status=200)}
    progressed = [wid for wid in before if before[wid] != after.get(wid)]
    # 只要求存活台推进：离线行的 last_heartbeat 冻结在容器消失那一刻，这是它的定义。
    ledger.check("空闲时心跳按轮询周期推进（>1s）", set(live_ids) <= set(progressed), f"before={before} after={after}")

    expect_error(ledger, "workers limit=201 → 422", lambda: api.get("/workers?limit=201"), 422)
    expect_error(ledger, "未知 worker drain → 404", lambda: api.post("/workers/no-such-worker/drain"), 404, contains="worker not found")
    expect_error(ledger, "未知 worker resume → 404", lambda: api.post("/workers/no-such-worker/resume"), 404, contains="worker not found")

    # --- drain / resume 单实例（要在存活台上做：离线台 drain 会被 409 拒绝）---
    w1 = live_ids[0]
    drained = api.post(f"/workers/{w1}/drain", expect_status=200)
    ledger.equal("drain 返回形状", sorted(drained), sorted(["id", "drain_requested", "status"]))
    ledger.equal("drain 置位", (drained["id"], drained["drain_requested"], drained["status"]), (w1, True, "draining"))
    time.sleep(3.0)
    seen = {w["id"]: w for w in api.get("/workers?limit=200", expect_status=200)}
    ledger.equal("Worker 侧消费 drain_requested（自报 draining）", seen[w1]["status"], "draining")
    resumed = api.post(f"/workers/{w1}/resume", expect_status=200)
    ledger.equal("resume 复位", (resumed["drain_requested"], resumed["status"]), (False, "online"))

    # --- drain-version 批量 ---
    batch = api.post("/workers/drain-version?version=0.1.0", expect_status=200)
    ledger.equal("drain-version 载荷字段", sorted(batch), sorted(["drained", "flagged"]))
    ledger.check("drain-version 命中全部在线 worker", batch["drained"] + batch["flagged"] >= 2, str(batch))
    api.post("/workers/drain-version?version=0.9.9", expect_status=200)
    ledger.equal("drain-version 不存在的版本不报错", {w["id"]: w["drain_requested"] for w in api.get("/workers?limit=200", expect_status=200)}.get(w1), True)
    expect_error(ledger, "drain-version 空 version → 422", lambda: api.post("/workers/drain-version?version="), 422)
    for wid in ids:
        if wid in live_ids:
            api.post(f"/workers/{wid}/resume", expect_status=200)
        else:
            # 离线行不能 resume：后端返回 409 "worker is offline"（UI 的恢复按钮 disabled 同源）。
            expect_error(ledger, f"离线行 {wid} resume → 409", lambda wid=wid: api.post(f"/workers/{wid}/resume"), 409, contains="worker is offline")
    # drain-version 会给离线机器记下标记（"重启后生效"），而离线行不能 resume，
    # 它的 drain_requested=True 是终态——无标记断言只对存活台成立。
    ledger.equal(
        "全部 resume 后无 drain 标记",
        any(w["drain_requested"] for w in api.get("/workers?limit=200", expect_status=200) if w["status"] != "offline"),
        False,
    )

    # --- 系统状态 / 概览 ---
    status = api.get("/system/status", expect_status=200)
    ledger.check("system/status 字段", {"file_tasks", "pending_oldest_seconds", "workers_online"} <= set(status), str(sorted(status)))
    ledger.equal("workers_online 与表内在线行数一致", status["workers_online"], len([w for w in api.get("/workers?limit=200", expect_status=200) if w["status"] in ("online", "busy", "draining")]))
    ledger.check("pending_oldest_seconds 空闲时为 null", status["pending_oldest_seconds"] is None or isinstance(status["pending_oldest_seconds"], (int, float)), str(status["pending_oldest_seconds"]))
    ledger.check("file_tasks 状态键都在词表内", set(status["file_tasks"]) <= {"pending", "running", "succeeded", "partial", "failed", "cancelled"}, str(status["file_tasks"]))

    overview = api.get("/system/overview", expect_status=200)
    ledger.check("overview 字段", {"jobs", "file_tasks", "matches_24h", "workers", "oldest_pending_seconds"} <= set(overview), str(sorted(overview)))
    ledger.check("overview.workers.by_version 含 0.1.0", overview["workers"]["by_version"].get("0.1.0", 0) >= 2, str(overview["workers"]))
    # matches_24h 有两个口径：概览的 SQL 聚合，和 /matches 的时间窗 scope。用后者独立数一遍
    # 再对账——只断言">0"在反复跑过的库上毫无意义（历史数据随便就把非零凑出来了）。
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    if overview["matches_24h"] > 60_000:
        ledger.skip("overview.matches_24h 与时间窗 scope 对账", f"命中量 {overview['matches_24h']} 过大，翻页拉全量不划算")
    else:
        window_rows = list(iter_matches(
            api, page=500,
            created_from=cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
            created_to="2999-12-31T23:59:59Z",
        ))
        ledger.equal("overview.matches_24h = 时间窗 scope 独立计数", overview["matches_24h"], len(window_rows))
    ledger.check("本轮抓取确实产生了命中", overview["matches_24h"] > 0, str(overview["matches_24h"]))
    ledger.check("overview.jobs 合计 >= 已建作业数", sum(overview["jobs"].values()) >= 5, str(overview["jobs"]))

    maint = api.post("/system/maintenance", expect_status=200)
    ledger.equal("maintenance 载荷字段", sorted(maint), sorted(["recovered", "workers_demoted", "jobs_finalized"]))
    ledger.check("空闲时维护三个计数为 0（幂等且无副作用）", all(v == 0 for v in maint.values()), str(maint))
    maint2 = api.post("/system/maintenance", expect_status=200)
    ledger.equal("maintenance 可重复执行", maint2, maint)
    return workers




# ============================================================================
# 编排
# ============================================================================
PHASES = ("health", "auth", "sources", "rulesets", "contract", "harvest", "matches", "reporting")


def main() -> int:
    parser = argparse.ArgumentParser(description="LogHarvest API 全流程自动化测试")
    parser.add_argument(
        "--phase",
        default="full",
        choices=[*PHASES, "full", "quick"],
        help="full=全部；quick=P0-P4+P6+P7（跳过耗时的抓取矩阵）；也可只跑单个阶段",
    )
    parser.add_argument("--keep", action="store_true", help="保留创建的作业（默认也只读，不删）")
    args = parser.parse_args()

    if args.phase in ("full", "quick"):
        wanted = set(PHASES) if args.phase == "full" else set(PHASES) - {"harvest"}
    else:
        wanted = {args.phase}
    # 前置依赖：后面的阶段都要 P0-P3 的身份与 id 映射。
    wanted |= {"health", "auth", "sources", "rulesets"}

    ledger = Ledger("API Flow")
    api = ApiClient(base=API_BASE)
    steps: list[tuple[str, Any]] = [
        ("health", lambda: phase_health(ledger, api)),
        ("auth", lambda: phase_auth(ledger, api)),
        ("sources", lambda: phase_data_sources(ledger, api)),
        ("rulesets", lambda: phase_rule_sets(ledger, api)),
        ("contract", lambda: phase_job_contract(ledger, api, CTX["ds_id"], CTX["rule_ids"])),
        ("harvest", lambda: phase_harvest(ledger, api, CTX["ds_id"], CTX["rule_ids"])),
        ("matches", lambda: phase_matches(ledger, api, CTX["ds_id"], CTX["rule_ids"])),
        ("reporting", lambda: phase_reporting(ledger, api)),
    ]
    CTX.update({"ds_id": None, "rule_ids": {}})
    for name, run in steps:
        if name not in wanted:
            continue
        try:
            result = run()
            if name == "sources":
                CTX["ds_id"] = result
            elif name == "rulesets":
                CTX["rule_ids"] = result
        except (ApiError, TimeoutError, KeyError, OSError) as exc:
            ledger.fail_phase(f"{name} 阶段异常", exc)
            print(f"!! 阶段 {name} 中断：{type(exc).__name__}: {exc}", flush=True)
            if name in ("health", "auth", "sources", "rulesets"):
                break
    summary = ledger.summary()
    return 0 if summary["failed"] == 0 else 1


CTX: dict[str, Any] = {}

if __name__ == "__main__":
    sys.exit(main())
