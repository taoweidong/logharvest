from fastapi.testclient import TestClient
from sqlmodel import Session

from src.domain.models.auth import User
from src.domain.services.passwords import verify_password

ADMIN_PASSWORD = "Test-Admin-Pass"
NEW_PASSWORD = "Brand-New-Pass-1"


def _role_id(client: TestClient, headers: dict[str, str], code: str) -> int:
    roles = client.get("/api/roles", headers=headers).json()
    return next(role["id"] for role in roles if role["code"] == code)


def _create_user(
    client: TestClient,
    headers: dict[str, str],
    username: str,
    *,
    password: str = "Passw0rd-123",
    nickname: str = "",
    role_ids: list[int] | None = None,
) -> dict:
    response = client.post(
        "/api/users",
        headers=headers,
        json={
            "username": username,
            "password": password,
            "nickname": nickname or username,
            "role_ids": role_ids or [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['accessToken']}"}


def _manager_headers(client: TestClient, headers: dict[str, str]) -> dict[str, str]:
    """创建一个只有 users:read/write 的非管理员，用来验证最后 admin 保护。"""
    menus = client.get("/api/menus", headers=headers, params={"limit": 500}).json()
    menu_ids = [menu["id"] for menu in menus if menu["name"] in {"users:read", "users:write"}]
    role = client.post(
        "/api/roles",
        headers=headers,
        json={"code": "user-manager", "name": "用户管理员"},
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["id"]
    assert (
        client.put(
            f"/api/roles/{role_id}/menus", headers=headers, json={"menu_ids": menu_ids}
        ).status_code
        == 204
    )
    _create_user(
        client,
        headers,
        "manager",
        password="Manager-Pass-1",
        role_ids=[role_id],
    )
    return _login(client, "manager", "Manager-Pass-1")


def test_admin_can_crud_users(client, auth_headers, session: Session) -> None:
    users = client.get("/api/users", headers=auth_headers)
    assert users.status_code == 200
    assert [user["username"] for user in users.json()] == ["admin"]
    assert users.json()[0]["roles"][0]["code"] == "admin"

    viewer_id = _role_id(client, auth_headers, "viewer")
    bob = _create_user(client, auth_headers, "bob", nickname="Bobby", role_ids=[viewer_id])
    assert bob["username"] == "bob"
    assert bob["nickname"] == "Bobby"
    assert [role["code"] for role in bob["roles"]] == ["viewer"]

    # keyword 大小写不敏感，用户名和昵称都能命中。
    by_username = client.get("/api/users", headers=auth_headers, params={"keyword": "BOB"}).json()
    by_nickname = client.get("/api/users", headers=auth_headers, params={"keyword": "bobby"}).json()
    assert [user["username"] for user in by_username] == ["bob"]
    assert [user["username"] for user in by_nickname] == ["bob"]

    updated = client.put(
        f"/api/users/{bob['id']}",
        headers=auth_headers,
        json={"nickname": "Robert", "is_active": False},
    )
    assert updated.status_code == 200
    assert updated.json()["nickname"] == "Robert"
    assert updated.json()["is_active"] is False

    operator_id = _role_id(client, auth_headers, "operator")
    reassigned = client.put(
        f"/api/users/{bob['id']}/roles",
        headers=auth_headers,
        json={"role_ids": [operator_id]},
    )
    assert reassigned.status_code == 200
    assert [role["code"] for role in reassigned.json()["roles"]] == ["operator"]

    reset = client.post(
        f"/api/users/{bob['id']}/reset-password",
        headers=auth_headers,
        json={"password": NEW_PASSWORD},
    )
    assert reset.status_code == 204
    assert reset.content == b""

    session.expire_all()
    stored = session.get(User, bob["id"])
    assert stored is not None
    assert verify_password(NEW_PASSWORD, stored.password_hash)
    assert not verify_password("Passw0rd-123", stored.password_hash)

    deleted = client.delete(f"/api/users/{bob['id']}", headers=auth_headers)
    assert deleted.status_code == 204
    remaining = client.get("/api/users", headers=auth_headers).json()
    assert [user["username"] for user in remaining] == ["admin"]


def test_create_user_rejects_duplicate_username_and_bad_password(client, auth_headers) -> None:
    _create_user(client, auth_headers, "bob")

    duplicate = client.post(
        "/api/users",
        headers=auth_headers,
        json={"username": "bob", "password": "Passw0rd-123"},
    )
    assert duplicate.status_code == 409

    short = client.post(
        "/api/users",
        headers=auth_headers,
        json={"username": "short-password", "password": "1234"},
    )
    assert short.status_code == 422

    missing_role = client.post(
        "/api/users",
        headers=auth_headers,
        json={"username": "ghost-role", "password": "Passw0rd-123", "role_ids": [999999]},
    )
    assert missing_role.status_code == 400


def test_user_cannot_disable_or_delete_themselves(client, auth_headers) -> None:
    me = client.get("/api/users", headers=auth_headers).json()[0]
    assert me["username"] == "admin"

    disable = client.put(f"/api/users/{me['id']}", headers=auth_headers, json={"is_active": False})
    assert disable.status_code == 400
    assert disable.json()["detail"] == "cannot disable yourself"

    delete = client.delete(f"/api/users/{me['id']}", headers=auth_headers)
    assert delete.status_code == 400
    assert delete.json()["detail"] == "cannot delete yourself"


def test_last_active_admin_is_protected(client, auth_headers) -> None:
    manager = _manager_headers(client, auth_headers)
    admin_role_id = _role_id(client, auth_headers, "admin")
    viewer_role_id = _role_id(client, auth_headers, "viewer")
    admin_id = client.get("/api/users", headers=auth_headers).json()[0]["id"]

    second = _create_user(
        client, auth_headers, "admin2", password="Admin2-Pass-1", role_ids=[admin_role_id]
    )

    # 两名 active admin 时，降级其中一个、删除降级后的用户都允许。
    demoted = client.put(
        f"/api/users/{second['id']}/roles",
        headers=manager,
        json={"role_ids": [viewer_role_id]},
    )
    assert demoted.status_code == 200
    assert [role["code"] for role in demoted.json()["roles"]] == ["viewer"]

    assert client.delete(f"/api/users/{second['id']}", headers=manager).status_code == 204

    # 只剩最后一个 active admin：摘除角色 / 删除都必须 409。
    demote_last = client.put(f"/api/users/{admin_id}/roles", headers=manager, json={"role_ids": []})
    assert demote_last.status_code == 409
    delete_last = client.delete(f"/api/users/{admin_id}", headers=manager)
    assert delete_last.status_code == 409

    # 再补一名 admin 后，降级又恢复允许（保护的是“最后一名”而不是管理员本身）。
    third = _create_user(
        client, manager, "admin3", password="Admin3-Pass-1", role_ids=[admin_role_id]
    )
    assert (
        client.put(
            f"/api/users/{admin_id}/roles", headers=manager, json={"role_ids": []}
        ).status_code
        == 200
    )
    disable_last = client.put(
        f"/api/users/{third['id']}", headers=manager, json={"is_active": False}
    )
    assert disable_last.status_code == 409
    assert client.delete(f"/api/users/{third['id']}", headers=manager).status_code == 409


def test_missing_user_returns_404(client, auth_headers) -> None:
    assert (
        client.put("/api/users/999999", headers=auth_headers, json={"nickname": "x"}).status_code
        == 404
    )
    assert (
        client.put(
            "/api/users/999999/roles", headers=auth_headers, json={"role_ids": []}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/users/999999/reset-password",
            headers=auth_headers,
            json={"password": NEW_PASSWORD},
        ).status_code
        == 404
    )
    assert client.delete("/api/users/999999", headers=auth_headers).status_code == 404


def test_viewer_is_forbidden_from_admin_read_endpoints(
    client, viewer_headers, auth_headers
) -> None:
    for path in ("/api/users", "/api/roles", "/api/menus"):
        assert client.get(path, headers=viewer_headers).status_code == 403
        assert client.get(path, headers=auth_headers).status_code == 200


def test_create_user_rejects_unusable_username(client, auth_headers) -> None:
    # 登录是按用户名精确匹配的：带空格或非 ASCII 的名字会造出「看起来重复、
    # 但在登录框里几乎敲不出来」的账号，因此在入口就拒绝，而不是等到登录后。
    before = len(client.get("/api/users", headers=auth_headers).json())
    for username in (" admin", "admin ", "管理员", "a", "ops/admin"):
        response = client.post(
            "/api/users",
            headers=auth_headers,
            json={"username": username, "password": NEW_PASSWORD, "role_ids": []},
        )
        assert response.status_code == 422, username
    assert len(client.get("/api/users", headers=auth_headers).json()) == before
