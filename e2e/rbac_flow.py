"""RBAC / 认证 / 审计 全流程自动化测试（真实 wslc 栈，纯 HTTP，不碰库）。

覆盖管理面：
- 认证边界：401 的四种文案、token 类型隔离、停用用户、登录限流滑动窗口；
- 用户 / 角色 / 菜单 CRUD 与自我保护、系统内置对象保护；
- 最小授权用户的逐接口 403（权限码 == menu_type==2 的节点名）与动态路由；
- 操作日志（append-only）：23 个 action 的触发证据、先审计后业务的事务回滚、
  删除用户后 actor_id 置空但快照保留、口令与敏感串绝不落进 detail。

断言的是契约里的**逐字文案**（见 e2e/refs/api-contract.md §2/§3/§10），不是"看起来
合理的状态码"。产品改了文案就该红，否则回归没有意义。

用法：
    service/.venv/Scripts/python.exe e2e/rbac_flow.py
    service/.venv/Scripts/python.exe e2e/rbac_flow.py --phase enforce
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from client import (  # noqa: E402
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    API_BASE,
    ApiClient,
    ApiError,
    Ledger,
)

PHASES = ["auth", "users", "roles", "menus", "enforce", "audit"]

# 契约 §10 逐字摘录的 23 个 action。本流程自己能触发的是前 15 个（auth/user/role/menu）；
# 其余 9 个分别由 api_flow（数据源、规则集、作业）与 runtime/limits_flow（worker、
# maintenance）覆盖，这里只做"存在性证据"统计，缺席记 SKIP 而不是假装通过。
OWN_ACTIONS = [
    "auth.login_success", "auth.login_failed", "auth.logout",
    "user.create", "user.update", "user.assign_roles", "user.reset_password", "user.delete",
    "role.create", "role.update", "role.delete", "role.assign_menus",
    "menu.create", "menu.update", "menu.delete",
]
OTHER_FLOW_ACTIONS = [
    "data_source.create", "rule_set.create_version",
    "job.create", "job.cancel", "job.retry_failed",
    "worker.drain", "worker.resume", "worker.drain_version", "system.maintenance",
]

PASSWORD = "E2eRbac#Pass1"  # 本地 e2e 占位口令，绝非生产凭据
MARKER = "e2e-rbac-sensitive-marker"  # 只出现在"口令"字段里，用来证明审计 detail 不落敏感串
MARKER_PASSWORD = f"{MARKER}9"


def suffix() -> str:
    """本轮运行的唯一后缀：所有新建对象都带上它，重复跑不冲突、也好清理。"""
    return time.strftime("%d%H%M%S", time.gmtime())


def probe(
    ledger: Ledger,
    api: ApiClient,
    name: str,
    method: str,
    path: str,
    payload: Any = None,
    *,
    status: int,
    detail: Any = None,
    contains: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """负面用例的统一形状：状态码 + detail 逐字（或子串）双断言。

    detail 传 list 断言形状（框架 422 是数组、业务错误是字符串），传 str 逐字相等，
    contains 用于带动态值的文案（如 ``unknown role ids: {999999}``）。
    """
    response = api.call(method, path, payload, **kw)
    body = response["body"]
    ok = ledger.equal(f"{name} 状态码", response["status"], status)
    got_detail = body.get("detail") if isinstance(body, dict) else None
    if detail is not None:
        if isinstance(detail, type):
            ledger.check(f"{name} detail 形状", isinstance(got_detail, detail), f"got={got_detail!r}")
        else:
            ledger.equal(f"{name} detail", got_detail, detail)
    if contains is not None:
        ledger.check(
            f"{name} detail 含 {contains!r}",
            contains in json.dumps(got_detail, ensure_ascii=False, default=str),
            f"detail={got_detail!r}",
        )
    if not ok:
        ledger.check(f"{name} 完整响应", False, json.dumps(body, ensure_ascii=False, default=str)[:300])
    return response


def status_only(ledger: Ledger, api: ApiClient, name: str, method: str, path: str, payload: Any = None, *, status: int) -> Any:
    """只关心状态码（204 空体这类），成功时返回 None。"""
    response = api.call(method, path, payload)
    ledger.equal(name, response["status"], status)
    if status == 204:
        ledger.check(f"{name} 空响应体", response["body"] in (None, ""), f"body={response['body']!r}")
    return response["body"]


def logs(api: ApiClient, **params: Any) -> list[dict[str, Any]]:
    """按条件翻页拉操作日志（audit 是分页裸数组，没有 total）。"""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        page = api.get(f"/operation-logs?limit=500&offset={offset}" + (f"&{query}" if query else ""))
        out.extend(page)
        if len(page) < 500:
            return out
        offset += 500


def count_action(api: ApiClient, action: str) -> int:
    return len(logs(api, action=action))


# --------------------------------------------------------------------------- P1
def phase_auth(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("P1 认证契约与 token 边界")

    anon = ApiClient(base=API_BASE)
    probe(ledger, anon, "匿名 GET /jobs", "GET", "/jobs", status=401, detail="missing bearer token")
    # 非 Bearer 的 Authorization 头：HTTPBearer(auto_error=False) 解析不出凭证，
    # _access_claims 统一按 "missing bearer token" 拒绝（dependencies.py:88）。
    probe(ledger, anon, "非 Bearer scheme", "GET", "/jobs",
          headers={"Authorization": "Basic Zm86YmFy"},
          status=401, detail="missing bearer token")
    probe(ledger, ApiClient(base=API_BASE, token="not.a.jwt"), "垃圾 token", "GET", "/jobs",
          status=401, detail="invalid or expired token")

    admin = ApiClient(base=API_BASE)
    token = admin.login(ADMIN_USERNAME, ADMIN_PASSWORD)
    ledger.check("管理员登录拿到 accessToken", bool(token), f"len={len(token)}")
    body = admin.post("/login", {"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}, expect_status=200)["data"]
    ledger.equal("登录 roles", body["roles"], ["admin"])
    ledger.equal("登录 permissions 走通配", body["permissions"], ["*:*:*"])
    ledger.check("expires 是毫秒 epoch", body["expires"] > 10**12, f"expires={body['expires']}")
    ledger.check("avatar 字段存在且为 null", "avatar" in body and body["avatar"] is None, body.get("avatar"))

    # token 类型隔离：refresh 不能当 access 用（HS256 里 type 参与校验）。
    refresh = body["refreshToken"]
    probe(ledger, ApiClient(base=API_BASE, token=refresh), "refresh token 当 bearer",
          "GET", "/jobs", status=401, detail="invalid or expired token")
    rotated = admin.refresh(refresh)
    again = admin.refresh(rotated["refreshToken"])
    ledger.check("refresh 换签的 accessToken 可用",
                 isinstance(ApiClient(base=API_BASE, token=rotated["accessToken"]).get("/jobs"), list))
    ledger.check("轮换出的 refresh 仍可再换签", bool(again["accessToken"]))
    ledger.skip("refreshToken 逐字轮换断言",
                "token 载荷只有 {sub,username,type,exp}、没有 jti/iat，同一秒内重复签发必然同串；"
                "断言「两次串不相等」会随时钟抖动变假失败，这里只验证「能换签且可用」")
    probe(ledger, anon, "空 refreshToken", "POST", "/refresh-token", {"refreshToken": "   "},
          status=422, detail=list, contains="refreshToken must not be blank")
    probe(ledger, anon, "垃圾 refreshToken", "POST", "/refresh-token", {"refreshToken": "abc.def.ghi"},
          status=401, detail="invalid refresh token")

    # 框架 422 是数组、业务 401 是字符串——两种形状都必须被区分开（契约 §0）。
    probe(ledger, anon, "登录体缺字段", "POST", "/login", {"username": "x"}, status=422, detail=list)
    probe(ledger, anon, "未知用户名也 401", "POST", "/login",
          {"username": f"nobody-{suffix()}", "password": PASSWORD}, status=401, detail="invalid credentials")
    probe(ledger, anon, "用户名对口令错", "POST", "/login",
          {"username": ADMIN_USERNAME, "password": "WrongPass#123"}, status=401, detail="invalid credentials")

    # 限流：滑动窗口只统计失败、按 用户名|IP 计数、成功登录清零（契约 §2.1）。
    victim = f"e2e-throttle-{suffix()}"
    hits = 0
    for _ in range(10):
        response = anon.call("POST", "/login", {"username": victim, "password": PASSWORD})
        hits += 1 if response["status"] == 401 else 0
    blocked = anon.call("POST", "/login", {"username": victim, "password": PASSWORD})
    ledger.equal("第 11 次失败触发 429", blocked["status"], 429)
    ledger.equal("限流 detail", blocked["body"].get("detail"), "too many login attempts; try again later")
    ledger.equal("失败计数与预期一致", hits, 10)
    retry_after = (blocked.get("headers") or {}).get("retry-after")
    ledger.check("429 带 Retry-After 且为秒数",
                 isinstance(retry_after, str) and retry_after.isdigit() and int(retry_after) >= 1,
                 f"Retry-After={retry_after!r}")
    probe(ledger, anon, "口令正确但账号不存在仍 401", "POST", "/login",
          {"username": f"ghost-{suffix()}", "password": PASSWORD}, status=401, detail="invalid credentials")

    # 动态路由：管理员通配 → 全量菜单树（前端只有 addRoutes，后端必须给成形的树）。
    # 节点形状见 navigation._render：{path, name, component, meta{title,icon,rank,...}}，
    # 目录多一层 children，页面额外带 meta.auths。
    routes = admin.get("/auth/routes", expect_status=200)["data"]
    ledger.check("管理员动态路由非空", isinstance(routes, list) and len(routes) > 0, f"roots={len(routes)}")
    flat = _flatten_routes(routes)
    paths = {str(node.get("path")) for node in flat}
    ledger.check("路由是树形（至少一个目录带 children）",
                 any(node.get("children") for node in routes),
                 f"roots={sorted({str(r.get('path')) for r in routes})}")
    ledger.check("路由含作业页面（页面藏在目录下）",
                 any("jobs" in p.lower() for p in paths), f"paths={sorted(paths)[:12]}")
    ledger.check("路由节点字段齐备",
                 all({"path", "name", "component", "meta"} <= set(node) for node in flat),
                 sorted({k for node in flat for k in node}))
    ledger.check("页面节点带 meta.auths",
                 any(isinstance((node.get("meta") or {}).get("auths"), list) for node in flat),
                 "目录节点无 auths 属正常")
    logout = admin.call("POST", "/logout")
    ledger.equal("登出返回信封 200", logout["status"], 200)
    ledger.equal("登出信封内容", logout["body"], {"code": 0, "message": "ok", "data": None})
    ledger.check("登出不吊销 token（无服务端会话）",
                 isinstance(admin.get("/jobs"), list), "JWT 自然过期，登出只留审计痕")
    ctx["admin"] = admin
    ctx["token"] = token


# --------------------------------------------------------------------------- P2
def phase_users(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("P2 用户 CRUD 与自我保护")
    tag = suffix()
    uname = f"e2e-user-{tag}"
    roles = {r["code"]: r["id"] for r in api.get("/roles")}
    ledger.check("种子角色齐备", {"admin", "operator", "viewer"} <= set(roles), f"codes={sorted(roles)}")

    created = api.post("/users", {
        "username": uname, "password": PASSWORD, "nickname": f"最小授权 {tag}", "role_ids": [roles["viewer"]],
    }, expect_status=201)
    ctx["user_id"] = created["id"]
    ctx["username"] = uname
    ctx.setdefault("made_users", []).append(created["id"])
    ledger.equal("新用户角色回显", [r["code"] for r in created["roles"]], ["viewer"])
    ledger.check("响应不含口令字段", "password" not in json.dumps(created), json.dumps(created)[:200])

    probe(ledger, api, "重名用户", "POST", "/users",
          {"username": uname, "password": PASSWORD}, status=409, detail="username already exists")
    probe(ledger, api, "口令不足 8 位", "POST", "/users",
          {"username": f"e2e-short-{tag}", "password": "abc"}, status=422, detail=list)
    probe(ledger, api, "用户名模式违规", "POST", "/users",
          {"username": "_bad start", "password": PASSWORD}, status=422, detail=list)
    probe(ledger, api, "未知角色 id", "POST", "/users",
          {"username": f"e2e-ghostrole-{tag}", "password": PASSWORD, "role_ids": [999_999]},
          status=400, detail=str, contains="unknown role ids")

    found = api.get(f"/users?keyword={tag}")
    ledger.equal("keyword 模糊命中", [u["username"] for u in found], [uname])
    ledger.check("列表无 total 包装（裸数组）", isinstance(found, list), type(found).__name__)

    me = next(u for u in api.get("/users?limit=500") if u["username"] == ADMIN_USERNAME)
    probe(ledger, api, "禁用自己", "PUT", f"/users/{me['id']}", {"is_active": False},
          status=400, detail="cannot disable yourself")
    probe(ledger, api, "删除自己", "DELETE", f"/users/{me['id']}", status=400, detail="cannot delete yourself")
    probe(ledger, api, "移除最后一个在职管理员的角色", "PUT", f"/users/{me['id']}/roles", {"role_ids": []},
          status=409, detail="cannot remove the last active admin")
    # 「最后一个在职管理员」两条守卫的可达路径不同：
    #   - PUT /users/{id}/roles 没有自我保护检查，admin 自己就能触发 409（上一条已验）；
    #   - DELETE /users/{id} 的自我保护检查排在前面（rbac.py:244 早于 :246），admin 只会
    #     拿到 cannot delete yourself，所以这条 409 只能由"受托者"命中。
    _last_admin_delete_guard(ledger, api, ctx, tag, me, roles)

    updated = api.put(f"/users/{ctx['user_id']}", {"nickname": f"改名-{tag}", "is_active": True})
    ledger.equal("改名生效", updated["nickname"], f"改名-{tag}")
    # 审计防泄漏的证据链：把一个"只会出现在口令里"的标记串当作口令重置目标，
    # 随后在 P6 里全量扫操作日志，断言这个串一次都没落进 detail/summary。
    status_only(ledger, api, "重置口令 204", "POST", f"/users/{ctx['user_id']}/reset-password",
                {"password": MARKER_PASSWORD}, status=204)

    temp = ApiClient(base=API_BASE)
    temp.login(uname, MARKER_PASSWORD)
    ledger.check("重置后新口令可登录", bool(temp.token))
    probe(ledger, temp, "viewer 访问 /users", "GET", "/users", status=403, detail="permission 'users:read' required")

    api.put(f"/users/{ctx['user_id']}", {"is_active": False})
    probe(ledger, temp, "停用后旧 token 失效", "GET", "/jobs", status=401, detail="user is not available")
    probe(ledger, ApiClient(base=API_BASE), "停用用户登录", "POST", "/login",
          {"username": uname, "password": MARKER_PASSWORD}, status=403, detail="user is disabled")
    api.put(f"/users/{ctx['user_id']}", {"is_active": True})
    api.put(f"/users/{ctx['user_id']}/roles", {"role_ids": [roles["operator"]]})
    after = next(u for u in api.get(f"/users?keyword={tag}") if u["username"] == uname)
    ledger.equal("整体替换角色生效", [r["code"] for r in after["roles"]], ["operator"])
    probe(ledger, api, "未知角色 id（替换）", "PUT", f"/users/{ctx['user_id']}/roles", {"role_ids": [999_998]},
          status=400, detail=str, contains="unknown role ids")
    probe(ledger, api, "不存在用户 404", "PUT", "/users/987654", {"nickname": "x"},
          status=404, detail="user not found")
    ctx["operator_user"] = {"username": uname, "password": MARKER_PASSWORD}
    ctx["roles"] = roles


def _find_user(api: ApiClient, username: str) -> dict[str, Any] | None:
    """没有 GET /users/{id}，只能按 keyword 捞（keyword 是 ILIKE 片段匹配）。"""
    return next((u for u in api.get(f"/users?keyword={username}") if u["username"] == username), None)


def _last_admin_delete_guard(ledger: Ledger, api: ApiClient, ctx: dict[str, Any], tag: str,
                             admin_row: dict[str, Any], roles: dict[str, int]) -> None:
    """受托者（只持 users:write 的非管理员）删除唯一在职管理员必须 409。

    这一步会真的向环境里唯一的管理员发起 DELETE，所以自带自愈通道：先建一个
    "停用状态的备援管理员"（``_is_last_active_admin`` 只数 active 用户，rbac.py:175），
    万一守卫失效、admin 被删，仍能由受托用户启用备援账号、重建 admin 并换回 token，
    不至于把整套 e2e 环境锁死。
    """
    perms = {m["name"]: m["id"] for m in api.get("/menus") if m["menu_type"] == 2}
    users_write = perms.get("users:write")
    if users_write is None:
        ledger.skip("受托者删除最后一个在职管理员", "菜单里缺 users:write 权限节点，授权面与契约不符")
        return

    backup = api.post("/users", {"username": f"e2e-backup-{tag}", "password": PASSWORD,
                                 "role_ids": [roles["admin"]]}, expect_status=201)
    ctx.setdefault("made_users", []).append(backup["id"])
    api.put(f"/users/{backup['id']}", {"is_active": False})
    row = _find_user(api, backup["username"])
    ledger.check("备援管理员已停用（不计入在职管理员）", row is not None and row["is_active"] is False, row)

    deleg_role = api.post("/roles", {"code": f"e2e_deleg_{tag}", "name": f"E2E 受托 {tag}"},
                          expect_status=201)
    ctx.setdefault("made_roles", []).append(deleg_role["id"])
    api.put(f"/roles/{deleg_role['id']}/menus", {"menu_ids": [users_write]})
    deleg = make_user(api, ctx, tag, "e2e-deleg", [deleg_role["id"]])
    ledger.equal("受托者 permissions 只有 users:write", deleg["login"]["permissions"], ["users:write"])

    # 先证明受托者确实删得动普通用户，否则下面的 409 可能只是"根本没权限"的假阳性。
    victim = api.post("/users", {"username": f"e2e-victim-{tag}", "password": PASSWORD, "role_ids": []},
                      expect_status=201)
    status_only(ledger, deleg["api"], "受托者删除普通用户", "DELETE", f"/users/{victim['id']}", status=204)

    response = deleg["api"].call("DELETE", f"/users/{admin_row['id']}")
    survived = _find_user(api, ADMIN_USERNAME)
    ledger.equal("受托者删除最后一个在职管理员被拒", response["status"], 409)
    ledger.equal("409 文案",
                 response["body"].get("detail") if isinstance(response["body"], dict) else None,
                 "cannot delete the last active admin")
    ledger.check("管理员账号仍然存在", survived is not None and survived["is_active"] is True, survived)
    if survived is None:
        _recover_admin(ledger, api, ctx, deleg, backup, roles["admin"])


def _recover_admin(ledger: Ledger, api: ApiClient, ctx: dict[str, Any], deleg: dict[str, Any],
                   backup: dict[str, Any], admin_role_id: int) -> None:
    """守卫失效（admin 真被删）后的自愈：启用备援 → 备援重建 admin → 共享客户端换 token。"""
    try:
        deleg["api"].put(f"/users/{backup['id']}", {"is_active": True})
        rescue = ApiClient(base=API_BASE)
        rescue.login(backup["username"], PASSWORD)
        recreated = rescue.post("/users", {"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD,
                                           "role_ids": [admin_role_id]}, expect_status=201)
        api.login(ADMIN_USERNAME, ADMIN_PASSWORD)
        ledger.check("已自愈出新的 admin 账号（产品缺陷：最后一个管理员守卫未生效）", True,
                     f"备援 {backup['username']} 重建 admin id={recreated['id']}")
    except (ApiError, OSError, KeyError) as exc:
        ledger.check("守卫失效且自愈失败，后续阶段将连锁变红", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- P3
def phase_roles(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("P3 角色 CRUD 与内置角色保护")
    tag = suffix()
    code = f"e2e_role_{tag}"
    all_menus = api.get("/menus")
    ctx["all_menus"] = all_menus
    roles = {r["code"]: r for r in api.get("/roles")}
    admin_role = roles["admin"]

    created = api.post("/roles", {"code": code, "name": f"E2E 最小角色 {tag}", "description": "e2e"},
                      expect_status=201)
    ctx["role_id"] = created["id"]
    ledger.equal("新角色 is_system", created["is_system"], False)
    ledger.equal("新角色默认激活", created["is_active"], True)
    ledger.equal("新角色 code", created["code"], code)

    probe(ledger, api, "角色码重复", "POST", "/roles", {"code": code, "name": "dup"},
          status=409, detail="role code already exists")
    probe(ledger, api, "角色码大写违规", "POST", "/roles", {"code": f"E2E_Bad_{tag}", "name": "x"},
          status=422, detail=list)

    probe(ledger, api, "内置角色停用", "PUT", f"/roles/{admin_role['id']}", {"is_active": False},
          status=400, detail="system role cannot be disabled")
    probe(ledger, api, "内置角色删除", "DELETE", f"/roles/{admin_role['id']}",
          status=409, detail="system role cannot be deleted")
    probe(ledger, api, "角色名空白", "PUT", f"/roles/{ctx['role_id']}", {"name": "   "},
          status=422, detail=list, contains="name must not be blank")
    renamed = api.put(f"/roles/{ctx['role_id']}", {"name": f"E2E 改名 {tag}"})
    ledger.equal("角色改名生效", renamed["name"], f"E2E 改名 {tag}")

    # 授权：权限码就是 menu_type==2 的节点名，授一个 jobs:read 进去。
    jobs_read = next(m for m in all_menus if m["name"] == "jobs:read")
    ctx["jobs_read_id"] = jobs_read["id"]
    ledger.equal("新建角色的菜单初始为空", api.get(f"/roles/{ctx['role_id']}/menus"), [])
    status_only(ledger, api, "授权菜单 204", "PUT", f"/roles/{ctx['role_id']}/menus",
                {"menu_ids": [jobs_read["id"]]}, status=204)
    ledger.equal("授权后回读", api.get(f"/roles/{ctx['role_id']}/menus"), [jobs_read["id"]])
    probe(ledger, api, "未知菜单 id", "PUT", f"/roles/{ctx['role_id']}/menus", {"menu_ids": [999_999]},
          status=400, detail=str, contains="unknown menu ids")
    # 系统角色的授权面走的是 /roles/{id}/menus（rbac.py:352 的 is_system 守卫只在这条路上）。
    # PUT /roles/{id} 是角色本身的更新接口，多余字段被 schema 忽略，别拿它当授权接口用。
    probe(ledger, api, "内置角色改菜单", "PUT", f"/roles/{admin_role['id']}/menus", {"menu_ids": []},
          status=409, detail="system role menus cannot be changed")
    probe(ledger, api, "不存在角色 404", "GET", "/roles/987654/menus", status=404, detail="role not found")

    # 特例语义：admin 角色不写 role_menus，GET menus 返回"全部菜单 id"（契约 §3.3 之 7）。
    admin_menus = api.get(f"/roles/{admin_role['id']}/menus")
    ledger.equal("admin 角色菜单=全量", sorted(admin_menus), sorted(m["id"] for m in all_menus))
    ledger.check("admin 角色无 role_menus 行也能通配", len(admin_menus) > 0, f"n={len(admin_menus)}")

    # 已分配用户的角色不能删（先分配、再删用户、最后删角色）。
    holder = api.post("/users", {"username": f"e2e-holder-{tag}", "password": PASSWORD,
                                 "role_ids": [ctx["role_id"]]}, expect_status=201)
    ctx["holder_id"] = holder["id"]
    probe(ledger, api, "删除已分配角色", "DELETE", f"/roles/{ctx['role_id']}",
          status=409, detail=str, contains="role is assigned to")
    status_only(ledger, api, "删除占位用户 204", "DELETE", f"/users/{holder['id']}", status=204)
    status_only(ledger, api, "解除分配后删除角色 204", "DELETE", f"/roles/{ctx['role_id']}", status=204)
    probe(ledger, api, "删除已删角色 404", "DELETE", f"/roles/{ctx['role_id']}",
          status=404, detail="role not found")
    ctx["role_id"] = None  # 已删除，清理阶段不再动它


# --------------------------------------------------------------------------- P4
def phase_menus(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("P4 菜单树约束与内置菜单保护")
    tag = suffix()

    def mk(**payload: Any) -> dict[str, Any]:
        return api.post("/menus", payload, expect_status=201)

    directory = mk(name=f"e2e-dir-{tag}", menu_type=0, title=f"E2E目录{tag}")
    page = mk(name=f"e2e-page-{tag}", menu_type=1, parent_id=directory["id"],
              path=f"/e2e-{tag}", component="e2e/Page", title=f"E2E页面{tag}")
    perm = mk(name=f"e2e-perm-{tag}", menu_type=2, parent_id=page["id"], title=f"E2E权限{tag}")
    ctx["made_menus"] = [directory, page, perm]
    ctx["page_only_menu_id"] = page["id"]
    ctx["page_path"] = page["path"]
    ledger.equal("权限节点 path 继承父页面", perm["path"], page["path"])
    ledger.equal("新建菜单非内置", perm["is_system"], False)

    probe(ledger, api, "根节点必须是目录", "POST", "/menus",
          {"name": f"e2e-rootpage-{tag}", "menu_type": 1, "path": "/x", "component": "x", "title": "t"},
          status=400, detail="root menu must be a directory")
    probe(ledger, api, "目录下不能挂权限节点", "POST", "/menus",
          {"name": f"e2e-permindir-{tag}", "menu_type": 2, "parent_id": directory["id"], "title": "t"},
          status=400, detail="directory children must be directories or pages")
    probe(ledger, api, "页面下只能挂权限节点", "POST", "/menus",
          {"name": f"e2e-dirinpage-{tag}", "menu_type": 0, "parent_id": page["id"], "title": "t"},
          status=400, detail="page children must be permission nodes")
    probe(ledger, api, "权限节点不能有子节点", "POST", "/menus",
          {"name": f"e2e-underperm-{tag}", "menu_type": 1, "parent_id": perm["id"],
           "path": "/y", "component": "y", "title": "t"},
          status=400, detail="permission menu cannot have children")
    probe(ledger, api, "目录不能带 component", "POST", "/menus",
          {"name": f"e2e-dircomp-{tag}", "menu_type": 0, "title": "t", "component": "bad"},
          status=400, detail="directory menu cannot have a component")
    probe(ledger, api, "页面缺 path/component", "POST", "/menus",
          {"name": f"e2e-pagebare-{tag}", "menu_type": 1, "parent_id": directory["id"], "title": "t"},
          status=400, detail="page menu requires path and component")
    probe(ledger, api, "权限节点不能带 component", "POST", "/menus",
          {"name": f"e2e-permcomp-{tag}", "menu_type": 2, "parent_id": page["id"],
           "title": "t", "component": "bad"},
          status=400, detail="permission menu cannot have a component")
    probe(ledger, api, "父节点不存在", "POST", "/menus",
          {"name": f"e2e-badparent-{tag}", "menu_type": 1, "parent_id": 987_654,
           "path": "/z", "component": "z", "title": "t"},
          status=400, detail="parent menu not found")
    probe(ledger, api, "菜单重名", "POST", "/menus",
          {"name": f"e2e-dir-{tag}", "menu_type": 0, "title": "dup"},
          status=409, detail="menu name already exists")
    probe(ledger, api, "menu_type 越界", "POST", "/menus",
          {"name": f"e2e-type3-{tag}", "menu_type": 3, "title": "t"}, status=422, detail=list)
    probe(ledger, api, "菜单名空白", "PUT", f"/menus/{directory['id']}", {"name": "  "},
          status=422, detail=list, contains="must not be blank")

    probe(ledger, api, "不能移动到自身", "PUT", f"/menus/{directory['id']}", {"parent_id": directory["id"]},
          status=400, detail="cannot move menu under itself or its descendant")
    probe(ledger, api, "不能移动到子孙下", "PUT", f"/menus/{directory['id']}", {"parent_id": page["id"]},
          status=400, detail="cannot move menu under itself or its descendant")
    probe(ledger, api, "移动到不存在父节点", "PUT", f"/menus/{page['id']}", {"parent_id": 987_653},
          status=400, detail="parent menu not found")

    # 内置菜单：只开放展示字段，name/path/component/parent_id 是前端路由契约的一部分。
    builtin_page = next(m for m in api.get("/menus") if m["is_system"] and m["menu_type"] == 1)
    for field, value in (("name", "hijacked"), ("path", "/hijacked"), ("component", "Hijack")):
        probe(ledger, api, f"内置菜单改 {field}", "PUT", f"/menus/{builtin_page['id']}", {field: value},
              status=400, detail=f"built-in menu cannot change {field}")
    probe(ledger, api, "内置菜单改父节点", "PUT", f"/menus/{builtin_page['id']}", {"parent_id": directory["id"]},
          status=400, detail="built-in menu cannot change parent_id")
    moved = api.put(f"/menus/{builtin_page['id']}", {"title": "E2E 临时标题"}, expect_status=200)
    ledger.equal("内置菜单可改展示字段 title", moved["title"], "E2E 临时标题")
    api.put(f"/menus/{builtin_page['id']}", {"title": builtin_page["title"]})
    after = next(m for m in api.get("/menus") if m["id"] == builtin_page["id"])
    ledger.equal("内置菜单 title 已还原", after["title"], builtin_page["title"])
    probe(ledger, api, "删除内置菜单", "DELETE", f"/menus/{builtin_page['id']}",
          status=409, detail="built-in menu cannot be deleted")

    # 先验树形（要用到 perm，后面的删除用例会把叶子拆掉）。
    tree = api.get("/menus/tree")
    node = next((n for n in tree if n["id"] == directory["id"]), None)
    ledger.check("平铺菜单能拼成树", node is not None and node["children"] and
                 node["children"][0]["children"][0]["id"] == perm["id"],
                 f"children={node and [c['id'] for c in node['children']]}")
    flat_ids = {m["id"] for m in api.get("/menus")}

    def walk(nodes: list[dict[str, Any]]) -> set[int]:
        out: set[int] = set()
        for n in nodes:
            out.add(n["id"])
            out |= walk(n.get("children") or [])
        return out

    ledger.equal("树与平铺的节点集合一致", walk(tree), flat_ids)

    # 权限节点是"激活的 menu_type==2 名字"集合，停用它必须立刻反映到鉴权（P5 验）。
    probe(ledger, api, "有子节点的目录不能删", "DELETE", f"/menus/{directory['id']}",
          status=409, detail="menu still has children")
    probe(ledger, api, "有子节点的页面不能删", "DELETE", f"/menus/{page['id']}",
          status=409, detail="menu still has children")
    # 叶子节点的删除通路（P6 的 menu.delete 审计证据就来自这里；cleanup 只做兜底）。
    status_only(ledger, api, "删除叶子权限节点 204", "DELETE", f"/menus/{perm['id']}", status=204)
    remaining = api.get("/menus")
    after_ids = {m["id"] for m in remaining}
    ledger.check("删除后菜单表不再包含该节点", perm["id"] not in after_ids, f"id={perm['id']}")
    probe(ledger, api, "重复删除菜单", "DELETE", f"/menus/{perm['id']}", status=404, detail="menu not found")
    # 摘掉唯一子节点后页面已无子引用，P7 cleanup 才删得掉它（P5 还要用它做授权面断言，这里不删）。
    ledger.check("子节点删除后父页面已无引用",
                 not any(m.get("parent_id") == page["id"] for m in remaining),
                 f"children_of_page={[m['id'] for m in remaining if m.get('parent_id') == page['id']]}")
    ctx["made_menus"] = [m for m in ctx.get("made_menus", []) if m["id"] != perm["id"]]
    probe(ledger, api, "不存在菜单 404", "PUT", "/menus/987651", {"title": "x"},
          status=404, detail="menu not found")

    ctx["dir_id"] = directory["id"]
    ctx["page_id"] = page["id"]
    ctx["perm_id"] = perm["id"]
# --------------------------------------------------------------------------- P5
PERMISSION_CODES = [
    "jobs:read", "jobs:write", "jobs:operate",
    "rulesets:read", "rulesets:write",
    "datasources:read", "datasources:write",
    "matches:read", "workers:read", "workers:operate",
    "system:read", "system:operate",
    "users:read", "users:write", "roles:read", "roles:write",
    "menus:read", "menus:write", "audit:read",
]


def make_role(api: ApiClient, ctx: dict[str, Any], tag: str, code: str, menu_ids: list[int]) -> int:
    role = api.post("/roles", {"code": f"{code}_{tag}", "name": f"E2E {code} {tag}"}, expect_status=201)
    ctx.setdefault("made_roles", []).append(role["id"])
    if menu_ids:
        api.put(f"/roles/{role['id']}/menus", {"menu_ids": menu_ids})
    return role["id"]


def make_user(api: ApiClient, ctx: dict[str, Any], tag: str, prefix: str, role_ids: list[int]) -> dict[str, Any]:
    username = f"{prefix}-{tag}"
    user = api.post("/users", {"username": username, "password": PASSWORD, "role_ids": role_ids},
                   expect_status=201)
    ctx.setdefault("made_users", []).append(user["id"])
    client = ApiClient(base=API_BASE)
    body = client.post("/login", {"username": username, "password": PASSWORD}, expect_status=200)["data"]
    # 绕过 ApiClient.login() 是为了同时拿到登录信封（roles/permissions 要断言）；
    # 代价必须补回来：不手工挂 token，后续这个用户的请求全是 401 missing bearer token。
    client.token = body["accessToken"]
    return {"id": user["id"], "username": username, "api": client, "login": body}


def phase_enforce(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("P5 最小授权矩阵与逐接口 403")
    tag = suffix()
    menus = [m for m in api.get("/menus") if not m["name"].startswith("e2e-")]
    perms = {m["name"]: m["id"] for m in menus if m["menu_type"] == 2}
    ledger.equal("内置权限节点=契约 19 码", sorted(perms), sorted(PERMISSION_CODES))

    jobs_only = make_role(api, ctx, tag, "e2e_jobs", [perms["jobs:read"]])
    viewer = make_role(api, ctx, tag, "e2e_viewer", [ctx["page_only_menu_id"]])  # 只给页面，不给权限
    u_jobs = make_user(api, ctx, tag, "e2e-jobs", [jobs_only])
    u_page = make_user(api, ctx, tag, "e2e-page", [viewer])
    u_none = make_user(api, ctx, tag, "e2e-norole", [])

    ledger.equal("单权限用户的 permissions", u_jobs["login"]["permissions"], ["jobs:read"])
    ledger.check("单权限用户 roles 只含自建角色", u_jobs["login"]["roles"] == [f"e2e_jobs_{tag}"],
                 u_jobs["login"]["roles"])
    ledger.check("jobs:read 用户可以列作业", isinstance(u_jobs["api"].get("/jobs"), list))
    for path, code in (
        ("/matches", "matches:read"), ("/users", "users:read"), ("/roles", "roles:read"),
        ("/menus", "menus:read"), ("/operation-logs", "audit:read"), ("/workers", "workers:read"),
        ("/data-sources", "datasources:read"), ("/rule-sets", "rulesets:read"), ("/rules", "rulesets:read"),
        ("/system/status", "system:read"), ("/stats/jobs", "jobs:read"),
    ):
        if code == "jobs:read":
            ledger.check(f"jobs:read 覆盖 {path}", isinstance(u_jobs["api"].get(path), (list, dict)), path)
            continue
        probe(ledger, u_jobs["api"], f"{path} 越权", "GET", path,
              status=403, detail=f"permission '{code}' required")
    probe(ledger, u_jobs["api"], "写作业越权", "POST", "/jobs", {"name": "x"},
          status=403, detail="permission 'jobs:write' required")
    probe(ledger, u_jobs["api"], "取消作业越权", "POST", "/jobs/1/cancel",
          status=403, detail="permission 'jobs:operate' required")
    # SSE 走 require_permission_streaming，403 文案与非流式一致（契约 §0）。
    probe(ledger, u_page["api"], "SSE 越权", "GET", "/jobs/1/events",
          status=403, detail="permission 'jobs:read' required")

    # 只授页面菜单 ≠ 授权限：路由里能看到页面，接口仍然 403。
    ledger.equal("页面型用户 permissions 为空", u_page["login"]["permissions"], [])
    routes = u_page["api"].get("/auth/routes", expect_status=200)["data"]
    paths = {node.get("path") for node in _flatten_routes(routes)}
    ledger.check("动态路由含被授的自建页面", ctx["page_path"] in paths, f"paths={sorted(str(p) for p in paths)}")
    ledger.check("动态路由不含未授权页面", not any(str(p or "").startswith("/system") for p in paths),
                 f"paths={sorted(str(p) for p in paths)}")
    probe(ledger, u_page["api"], "页面型用户列作业", "GET", "/jobs",
          status=403, detail="permission 'jobs:read' required")
    no_route = u_none["api"].get("/auth/routes", expect_status=200)["data"]
    ledger.equal("无角色用户路由为空", no_route, [])
    ledger.equal("无角色用户 permissions", u_none["login"]["permissions"], [])

    # 角色停用 / 权限节点停用都要即时生效（鉴权是每请求解析，不带缓存）。
    probe(ledger, u_jobs["api"], "停用前可列作业", "GET", "/jobs", status=200)
    api.put(f"/roles/{jobs_only}", {"is_active": False})
    probe(ledger, u_jobs["api"], "角色停用后失权", "GET", "/jobs",
          status=403, detail="permission 'jobs:read' required")
    api.put(f"/roles/{jobs_only}", {"is_active": True})
    ledger.check("角色复原后重新生效", isinstance(u_jobs["api"].get("/jobs"), list))
    jobs_read_id = perms["jobs:read"]
    api.put(f"/menus/{jobs_read_id}", {"is_active": False})
    try:
        probe(ledger, u_jobs["api"], "权限节点停用后失权", "GET", "/jobs",
              status=403, detail="permission 'jobs:read' required")
    finally:
        api.put(f"/menus/{jobs_read_id}", {"is_active": True})
    ledger.check("权限节点复原后重新生效", isinstance(u_jobs["api"].get("/jobs"), list))

    # 内置角色的授权面（operator 无 users:read / audit 有；viewer 只读且无 audit:read）。
    operator = {r["code"]: r["id"] for r in api.get("/roles")}["operator"]
    seed_viewer = {r["code"]: r["id"] for r in api.get("/roles")}["viewer"]
    u_op = make_user(api, ctx, tag, "e2e-op", [operator])
    u_vw = make_user(api, ctx, tag, "e2e-vw", [seed_viewer])
    ledger.check("operator 可读审计", isinstance(u_op["api"].get("/operation-logs"), list))
    probe(ledger, u_op["api"], "operator 无 users:read", "GET", "/users",
          status=403, detail="permission 'users:read' required")
    probe(ledger, u_op["api"], "operator 无 rulesets:write", "POST", "/rule-sets", {"name": "x"},
          status=403, detail="permission 'rulesets:write' required")
    probe(ledger, u_vw["api"], "viewer 无 audit:read", "GET", "/operation-logs",
          status=403, detail="permission 'audit:read' required")
    probe(ledger, u_vw["api"], "viewer 不能建作业", "POST", "/jobs", {"name": "x"},
          status=403, detail="permission 'jobs:write' required")
    ledger.check("viewer 可读 workers", isinstance(u_vw["api"].get("/workers"), list))
    # 一个用户多角色 = 权限并集（去重排序）。
    both = make_role(api, ctx, tag, "e2e_extra", [perms["matches:read"]])
    api.put(f"/users/{u_vw['id']}/roles", {"role_ids": [seed_viewer, both]})
    multi = u_vw["api"].post("/login", {"username": u_vw["username"], "password": PASSWORD},
                             expect_status=200)["data"]
    ledger.equal("多角色取并集", multi["permissions"], sorted({"jobs:read", "rulesets:read",
                                                               "datasources:read", "matches:read",
                                                               "workers:read", "system:read"}))
    ctx["u_jobs"] = u_jobs


def _flatten_routes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in nodes or []:
        out.append(node)
        out.extend(_flatten_routes(node.get("children") or []))
    return out


# --------------------------------------------------------------------------- P6
def phase_audit(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("P6 操作日志：证据、事务回滚、脱敏、快照")

    for action in OWN_ACTIONS:
        count = count_action(api, action)
        ledger.check(f"action {action} 有证据", count > 0, f"rows={count}")
    for action in OTHER_FLOW_ACTIONS:
        count = count_action(api, action)
        if count == 0:
            ledger.skip(f"action {action} 有证据", "本流程不触发，由 api_flow / runtime_flow 覆盖")
        else:
            ledger.check(f"action {action} 有证据（跨流程累计）", True, f"rows={count}")

    rows = logs(api, action="user")
    ledger.check("action 过滤是 ILIKE 前缀", all(str(r["action"]).startswith("user.") for r in rows),
                 f"n={len(rows)} actions={sorted({r['action'] for r in rows})}")
    ledger.check("resource_id 在 DTO 里是字符串",
                 all(isinstance(r["resource_id"], str) for r in rows[:20]),
                 {type(r["resource_id"]).__name__ for r in rows[:20]})

    target = ctx.get("user_id")
    if target:
        # 变更前面貌 + 变更内容都必须在 detail 里（before/after 是审计的核心价值）。
        api.put(f"/users/{target}", {"nickname": "审计证据改名"})
        newest = logs(api, action="user.update", resource_id=target)[0]
        detail = newest.get("detail") or {}
        ledger.equal("user.update detail.before 有账号名", detail.get("before", {}).get("username"),
                     ctx.get("username"))
        ledger.equal("user.update detail.after=请求增量", detail.get("after"),
                     {"nickname": "审计证据改名"})
        ledger.check("detail 里没有口令字段", "password" not in json.dumps(detail, ensure_ascii=False),
                     json.dumps(detail, ensure_ascii=False)[:200])
        created_rows = [r for r in logs(api, action="user.create")
                        if str(r.get("resource_id")) == str(target)]
        ledger.check("user.create 只记 after（新 id 业务提交后才存在）",
                     len(created_rows) == 1 and "before" not in (created_rows[0].get("detail") or {}),
                     created_rows[0].get("detail") if created_rows else "无行")

    me = next(u for u in api.get("/users?limit=500") if u["username"] == ADMIN_USERNAME)
    for action, call in (
        ("user.assign_roles", lambda: api.call("PUT", f"/users/{me['id']}/roles", {"role_ids": []})),
        ("user.update", lambda: api.call("PUT", f"/users/{me['id']}", {"is_active": False})),
    ):
        before = count_action(api, action)
        response = call()
        after = count_action(api, action)
        ledger.check(f"{action} 业务失败时返回 4xx", response["status"] in (400, 409),
                     f"status={response['status']}")
        ledger.equal(f"{action}：回滚不留审计行", after, before)

    r = api.call("DELETE", "/operation-logs/1")
    ledger.check("操作日志无删除接口（只追加）", r["status"] in (404, 405), f"status={r['status']}")
    r = api.call("POST", "/operation-logs", {"action": "fake"})
    ledger.check("操作日志无写入接口（只追加）", r["status"] in (404, 405), f"status={r['status']}")

    probe(ledger, api, "created_from 必须成对", "GET", "/operation-logs?created_from=2020-01-01T00:00:00",
          status=422, detail="created_from and created_to must be provided together")
    probe(ledger, api, "时间窗方向校验", "GET",
          "/operation-logs?created_from=2030-01-01T00:00:00&created_to=2020-01-01T00:00:00",
          status=422, detail="created_from must not be after created_to")
    window = logs(api, created_from="2020-01-01T00:00:00", created_to="2099-01-01T00:00:00")
    ledger.check("合法时间窗能拉到数据", len(window) >= len(logs(api, action="user.create")),
                 f"window={len(window)}")
    page1 = api.get("/operation-logs?limit=1")
    page2 = api.get("/operation-logs?limit=1&offset=1")
    ledger.check("limit/offset 生效且无 total 包装",
                 len(page1) == 1 and page1[0]["id"] != page2[0]["id"], f"{page1[0]['id']}/{page2[0]['id']}")
    probe(ledger, api, "limit 越界", "GET", "/operation-logs?limit=501", status=422, detail=list)

    # 快照语义：删除用户后 actor_id 置空，actor_name/role_hint 仍可读（append-only）。
    temp = make_user(api, ctx, suffix(), "e2e-audit", [])
    survivor_rows = [r for r in logs(api, action="auth.login_success") if r["actor_id"] == temp["id"]]
    ledger.check("登录成功审计带 actor_id", len(survivor_rows) >= 1, f"rows={len(survivor_rows)}")
    hints = {r.get("role_hint") for r in logs(api, action="auth.login_success")
             if r["actor_name"] == ADMIN_USERNAME}
    ledger.check("admin 登录 role_hint 快照", "admin" in hints, f"hints={hints}")
    api.call("DELETE", f"/users/{temp['id']}")
    after_rows = [r for r in logs(api, action="auth.login_success") if r["actor_id"] == temp["id"]]
    ledger.equal("删除用户后 actor_id 全部置空", len(after_rows), 0)
    kept = [r for r in logs(api, action="auth.login_success") if r["actor_name"] == temp["username"]]
    ledger.equal("置空后快照行仍保留", len(kept), len(survivor_rows))

    everything = json.dumps(logs(api), ensure_ascii=False)
    for secret in (MARKER, PASSWORD, ADMIN_PASSWORD):
        ledger.check(f"审计 detail 不含敏感串 {secret[:8]}…", secret not in everything,
                     f"命中长度={everything.count(secret)}")
    ledger.check("origin_ip 字段存在", all("origin_ip" in r for r in logs(api, action="role.create")),
                 "缺字段")
    ctx["audit_rows"] = len(logs(api))


def cleanup(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    """把本轮造出来的人/角色/菜单清干净，让下一次运行从同一状态开始。"""
    ledger.phase("P7 现场清理")
    for user_id in reversed(ctx.get("made_users", [])):
        api.call("DELETE", f"/users/{user_id}")
    for role_id in reversed(ctx.get("made_roles", [])):
        api.call("DELETE", f"/roles/{role_id}")
    for menu in reversed(ctx.get("made_menus", [])):
        api.call("DELETE", f"/menus/{menu['id']}")
    users_left = [u["username"] for u in api.get("/users?limit=500") if u["username"].startswith("e2e-")]
    roles_left = [r["code"] for r in api.get("/roles") if r["code"].startswith("e2e_")]
    menus_left = [m["name"] for m in api.get("/menus") if m["name"].startswith("e2e-")]
    ledger.equal("无 e2e 用户残留", users_left, [])
    ledger.equal("无 e2e 角色残留", roles_left, [])
    ledger.equal("无 e2e 菜单残留", menus_left, [])
    seed = {r["code"] for r in api.get("/roles")}
    ledger.check("内置角色未被误删", {"admin", "operator", "viewer"} <= seed, f"codes={sorted(seed)}")
    perm_nodes = [m["name"] for m in api.get("/menus") if m["menu_type"] == 2 and not m["name"].startswith("e2e-")]
    ledger.equal("权限节点数量复原", sorted(perm_nodes), sorted(PERMISSION_CODES))
    jobs_read = next((m for m in api.get("/menus") if m["name"] == "jobs:read"), None)
    ledger.check("jobs:read 节点恢复激活", jobs_read is not None and jobs_read["is_active"], jobs_read)


def main() -> int:
    parser = argparse.ArgumentParser(description="LogHarvest RBAC/认证/审计 全流程自动化测试")
    parser.add_argument("--phase", default="full", choices=[*PHASES, "full"],
                        help="full=全部；单阶段会自动带上前置阶段")
    args = parser.parse_args()
    order = ["auth", "users", "roles", "menus", "enforce", "audit"]
    if args.phase == "full":
        wanted = set(order)
    else:
        wanted = set(order[: order.index(args.phase) + 1]) | {"auth", "users"}

    ledger = Ledger("RBAC Flow")
    api = ApiClient(base=API_BASE)
    api.login(ADMIN_USERNAME, ADMIN_PASSWORD)
    ctx: dict[str, Any] = {"admin": api}
    handlers = {
        "auth": phase_auth, "users": phase_users, "roles": phase_roles,
        "menus": phase_menus, "enforce": phase_enforce, "audit": phase_audit,
    }
    for name in order:
        if name not in wanted:
            continue
        try:
            handlers[name](ledger, api, ctx)
        except (ApiError, KeyError, OSError, StopIteration, TypeError) as exc:
            ledger.fail_phase(f"{name} 阶段异常", exc)
            print(f"!! 阶段 {name} 中断：{type(exc).__name__}: {exc}", flush=True)
            if name in ("auth", "users"):
                break
    cleanup(ledger, api, ctx)
    summary = ledger.summary()
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

