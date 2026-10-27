"""操作日志（审计）的接口与服务用例。

覆盖三件事：埋点是否落到同一张表、口令一类敏感值是否真的没进来、查询接口的权限与
过滤契约。删号那条用例锁的是 SQLite 与 PostgreSQL 的真实差异——SQLite 驱动默认不启用
外键约束，所以 ``ON DELETE SET NULL`` 不会执行，置空由服务层的删除路径显式完成。
"""

import json
from datetime import datetime

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from src.application.services.audit import list_operation_logs
from src.domain.models.audit import OperationLog
from src.domain.models.auth import Role, User
from src.domain.services.passwords import hash_password

ADMIN_PASSWORD = "Test-Admin-Pass"
VIEWER_PASSWORD = "Viewer-Pass-123"
NEW_USER_PASSWORD = "Kate-Pass-123"
RESET_PASSWORD = "Rotate-Pass-123"
WRONG_PASSWORD = "Wrong-Pass-9999"
ALL_PASSWORDS = (ADMIN_PASSWORD, VIEWER_PASSWORD, NEW_USER_PASSWORD, RESET_PASSWORD, WRONG_PASSWORD)


def _rows(session: Session, *, action: str | None = None) -> list[OperationLog]:
    """走服务层读；请求用的是另一条会话，身份映射里可能是旧值，先过期再查。"""
    session.expire_all()
    return list_operation_logs(
        session,
        limit=500,
        offset=0,
        actor_id=None,
        action=action,
        resource_type=None,
        resource_id=None,
        created_from=None,
        created_to=None,
    )


def _only(session: Session, action: str) -> OperationLog:
    rows = _rows(session, action=action)
    assert len(rows) == 1, [row.summary for row in rows]
    return rows[0]


def _login(client: TestClient, username: str, password: str) -> int:
    return client.post("/api/login", json={"username": username, "password": password}).status_code


# --- 登录事件 ---------------------------------------------------------------


def test_login_success_and_failure_rows_differ_in_actor_id(
    client: TestClient, session: Session, admin_user: User
) -> None:
    assert _login(client, "admin", ADMIN_PASSWORD) == 200
    assert _login(client, "admin", WRONG_PASSWORD) == 401

    rows = _rows(session, action="auth.")
    success = next(row for row in rows if row.action == "auth.login_success")
    failure = next(row for row in rows if row.action == "auth.login_failed")

    assert success.actor_id == admin_user.id
    assert success.actor_name == "admin"
    # role_hint 是登录那一刻的角色码快照，逗号拼接。
    assert success.role_hint == "admin"
    assert failure.origin_ip == success.origin_ip

    # 失败没有可绑定的主体，但用户名快照必须留下，否则失败记录只是一串匿名 401。
    assert failure.actor_id is None
    assert failure.actor_name == "admin"
    assert failure.detail["reason"] == "口令不符"


def test_disabled_account_login_is_recorded_as_failure(
    client: TestClient, session: Session
) -> None:
    from src.application.services.auth import ensure_admin

    user = ensure_admin(session, "audit-sleepy", "Sleepy-Pass-1")
    user.is_active = False
    session.add(user)
    session.commit()

    assert _login(client, "audit-sleepy", "Sleepy-Pass-1") == 403

    row = _rows(session, action="auth.login_failed")[-1]
    assert row.actor_id is None
    assert row.actor_name == "audit-sleepy"
    assert row.detail["reason"] == "账号停用"


def test_logout_is_recorded(client: TestClient, session: Session, admin_user: User) -> None:
    token = client.post(
        "/api/login", json={"username": "admin", "password": ADMIN_PASSWORD}
    ).json()["data"]["accessToken"]

    assert (
        client.post("/api/logout", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    )

    row = _only(session, "auth.logout")
    assert row.actor_id == admin_user.id
    assert row.actor_name == "admin"
    assert row.detail == {}


# --- 写端点埋点 -------------------------------------------------------------


def test_user_create_row_and_detail(
    client: TestClient, session: Session, auth_headers: dict[str, str]
) -> None:
    created = client.post(
        "/api/users",
        headers=auth_headers,
        json={
            "username": "audit-kate",
            "password": NEW_USER_PASSWORD,
            "nickname": "Kate",
            "role_ids": [],
        },
    )
    assert created.status_code == 201

    row = _only(session, "user.create")
    assert row.resource_type == "user"
    assert row.resource_id == str(created.json()["id"])
    assert row.actor_name == "admin"
    assert row.summary == "创建用户 audit-kate"
    assert row.detail["after"] == {
        "username": "audit-kate",
        "nickname": "Kate",
        "is_active": True,
        "role_ids": [],
    }


def test_user_assign_roles_detail_carries_before_and_after(
    client: TestClient, session: Session, auth_headers: dict[str, str]
) -> None:
    operator_id = session.exec(select(Role.id).where(Role.code == "operator")).one()
    created = client.post(
        "/api/users",
        headers=auth_headers,
        json={"username": "audit-roles", "password": NEW_USER_PASSWORD},
    )
    user_id = created.json()["id"]

    moved = client.put(
        f"/api/users/{user_id}/roles",
        headers=auth_headers,
        json={"role_ids": [operator_id]},
    )
    assert moved.status_code == 200

    row = _only(session, "user.assign_roles")
    assert row.resource_id == str(user_id)
    assert row.detail["before_role_ids"] == []
    assert row.detail["after_role_ids"] == [operator_id]


def test_menu_update_detail_is_field_scoped(
    client: TestClient, session: Session, auth_headers: dict[str, str]
) -> None:
    menus = client.get("/api/menus", headers=auth_headers, params={"limit": 500}).json()
    jobs = next(menu for menu in menus if menu["name"] == "Jobs")

    moved = client.put(f"/api/menus/{jobs['id']}", headers=auth_headers, json={"title": "审计改名"})
    assert moved.status_code == 200

    row = _only(session, "menu.update")
    assert row.resource_type == "menu"
    assert row.resource_id == str(jobs["id"])
    assert row.actor_id is not None
    # before 只带被改的字段（外加用于认人的 name），不是整行镜像。
    assert row.detail["before"] == {"name": "Jobs", "title": jobs["title"]}
    assert row.detail["after"] == {"title": "审计改名"}


def test_failed_business_write_leaves_no_audit_row(
    client: TestClient, session: Session, auth_headers: dict[str, str]
) -> None:
    """业务被服务层拒绝时必须回滚掉待提交的审计行：没有发生的操作不能留案。"""
    duplicated = client.post(
        "/api/users",
        headers=auth_headers,
        json={"username": "admin", "password": NEW_USER_PASSWORD},
    )
    assert duplicated.status_code == 409

    assert _rows(session, action="user.create") == []


def test_reset_password_records_without_the_password(
    client: TestClient, session: Session, auth_headers: dict[str, str]
) -> None:
    created = client.post(
        "/api/users",
        headers=auth_headers,
        json={"username": "audit-reset", "password": NEW_USER_PASSWORD},
    )
    user_id = created.json()["id"]

    reset = client.post(
        f"/api/users/{user_id}/reset-password",
        headers=auth_headers,
        json={"password": RESET_PASSWORD},
    )
    assert reset.status_code == 204

    row = _only(session, "user.reset_password")
    assert row.summary == "重置口令"
    assert row.detail == {}
    assert row.resource_id == str(user_id)


def test_no_plaintext_password_in_any_audit_field(
    client: TestClient, session: Session, auth_headers: dict[str, str]
) -> None:
    """把口令写进审计是这套系统最贵的一次泄漏：明文一律不得落在任何字段里。"""
    _login(client, "admin", ADMIN_PASSWORD)
    _login(client, "admin", WRONG_PASSWORD)
    _login(client, "ghost", WRONG_PASSWORD)
    created = client.post(
        "/api/users",
        headers=auth_headers,
        json={"username": "audit-secret", "password": NEW_USER_PASSWORD},
    )
    user_id = created.json()["id"]
    client.put(f"/api/users/{user_id}", headers=auth_headers, json={"nickname": "改名"})
    client.post(
        f"/api/users/{user_id}/reset-password",
        headers=auth_headers,
        json={"password": RESET_PASSWORD},
    )
    client.post("/api/logout", headers=auth_headers)

    rows = list(session.exec(select(OperationLog)).all())
    assert rows
    for row in rows:
        blob = json.dumps(
            {
                "actor_name": row.actor_name,
                "role_hint": row.role_hint,
                "action": row.action,
                "resource_id": row.resource_id,
                "summary": row.summary,
                "detail": row.detail,
            },
            ensure_ascii=False,
        )
        for password in ALL_PASSWORDS:
            assert password not in blob, (row.action, password)


# --- 查询接口 ---------------------------------------------------------------


def test_viewer_is_forbidden_and_admin_is_allowed(
    client: TestClient, auth_headers: dict[str, str], viewer_headers: dict[str, str]
) -> None:
    """viewer 看不到操作日志：里面记着"谁改过哪个账号"，等于权限拓扑泄露。"""
    assert client.get("/api/operation-logs", headers=viewer_headers).status_code == 403
    assert client.get("/api/operation-logs").status_code == 401

    allowed = client.get("/api/operation-logs", headers=auth_headers)
    assert allowed.status_code == 200
    assert isinstance(allowed.json(), list)


def test_operator_can_read_operation_logs(
    client: TestClient, operator_headers: dict[str, str]
) -> None:
    assert client.get("/api/operation-logs", headers=operator_headers).status_code == 200


def test_action_prefix_filter_and_escaped_wildcards(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    client.post(
        "/api/users",
        headers=auth_headers,
        json={"username": "audit-filter", "password": NEW_USER_PASSWORD},
    )

    hits = client.get(
        "/api/operation-logs", headers=auth_headers, params={"action": "user.", "limit": 200}
    ).json()
    assert hits, "user. 前缀应当命中整族用户侧动作"
    assert {row["action"] for row in hits} <= {"user.create", "user.update", "user.delete"}

    # 调用方给的 % / _ 是字面量而不是通配符：漏了转义就等于给过滤开了一个全表口子。
    escaped = client.get(
        "/api/operation-logs", headers=auth_headers, params={"action": "user%"}
    ).json()
    assert escaped == []


def test_created_window_requires_a_pair(client: TestClient, auth_headers: dict[str, str]) -> None:
    only_from = client.get(
        "/api/operation-logs",
        headers=auth_headers,
        params={"created_from": "2026-10-27T00:00:00"},
    )
    assert only_from.status_code == 422
    assert only_from.json()["detail"] == "created_from and created_to must be provided together"

    only_to = client.get(
        "/api/operation-logs",
        headers=auth_headers,
        params={"created_to": "2026-10-28T00:00:00"},
    )
    assert only_to.status_code == 422

    reversed_window = client.get(
        "/api/operation-logs",
        headers=auth_headers,
        params={"created_from": "2026-10-28T00:00:00", "created_to": "2026-10-27T00:00:00"},
    )
    assert reversed_window.status_code == 422
    assert reversed_window.json()["detail"] == "created_from must not be after created_to"

    paired = client.get(
        "/api/operation-logs",
        headers=auth_headers,
        params={"created_from": "2020-01-01T00:00:00", "created_to": "2999-01-01T00:00:00"},
    )
    assert paired.status_code == 200


def test_actor_id_filter_only_returns_that_actor(
    client: TestClient, auth_headers: dict[str, str], viewer_user: User
) -> None:
    assert _login(client, "viewer", VIEWER_PASSWORD) == 200

    rows = client.get(
        "/api/operation-logs",
        headers=auth_headers,
        params={"actor_id": viewer_user.id, "limit": 200},
    ).json()
    assert rows, "viewer 登录后应至少留下一条 login_success"
    assert {row["actor_id"] for row in rows} == {viewer_user.id}
    assert all(row["actor_name"] == "viewer" for row in rows)


def test_rows_are_newest_first(client: TestClient, auth_headers: dict[str, str]) -> None:
    for index in range(3):
        client.post(
            "/api/users",
            headers=auth_headers,
            json={"username": f"audit-order{index}", "password": NEW_USER_PASSWORD},
        )

    stamps = [
        (datetime.fromisoformat(row["created_at"]), row["id"])
        for row in client.get(
            "/api/operation-logs", headers=auth_headers, params={"limit": 500}
        ).json()
    ]
    assert stamps == sorted(stamps, key=lambda item: (item[0], item[1]), reverse=True)


# --- 删号后的可读性 ---------------------------------------------------------


def test_deleted_user_rows_keep_the_name_but_lose_the_id(
    client: TestClient, session: Session, auth_headers: dict[str, str]
) -> None:
    """actor_id 置空、actor_name 保留：删号不得抹掉责任链。"""
    created = client.post(
        "/api/users",
        headers=auth_headers,
        json={"username": "audit-gone", "password": NEW_USER_PASSWORD},
    )
    user_id = created.json()["id"]
    assert _login(client, "audit-gone", NEW_USER_PASSWORD) == 200

    before = client.get(
        "/api/operation-logs",
        headers=auth_headers,
        params={"actor_id": user_id, "limit": 200},
    ).json()
    assert any(row["action"] == "auth.login_success" for row in before)

    assert client.delete(f"/api/users/{user_id}", headers=auth_headers).status_code == 204

    survivors = [row for row in _rows(session) if row.actor_name == "audit-gone"]
    assert survivors, "删号后仍要读得出是谁做过这些事"
    assert all(row.actor_id is None for row in survivors)
    assert {row.action for row in survivors} == {"auth.login_success"}

    # 删除本身那一行挂在管理员名下，不受影响。
    deletion = _only(session, "user.delete")
    assert deletion.resource_id == str(user_id)
    assert deletion.actor_name == "admin"


def test_service_detaches_rows_without_relying_on_the_database_fk(session: Session) -> None:
    """直接验证服务层置空：不依赖外键，也不管 SQLite 是否启用约束。"""
    from src.application.services.audit import detach_actor, record_operation

    user = User(username="audit-detached", password_hash=hash_password("Detached-Pass-1"))
    session.add(user)
    session.commit()
    session.refresh(user)
    record_operation(
        session,
        actor=user,
        action="auth.login_success",
        resource_type="user",
        resource_id=user.id,
        summary="登录成功",
    )
    session.commit()

    detach_actor(session, actor_id=user.id or 0)
    session.commit()

    row = _only(session, "auth.login_success")
    assert row.actor_id is None
    assert row.actor_name == "audit-detached"
