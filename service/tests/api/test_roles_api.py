from fastapi.testclient import TestClient


def _roles(client: TestClient, headers: dict[str, str]) -> dict[str, dict]:
    response = client.get("/api/roles", headers=headers)
    assert response.status_code == 200
    return {role["code"]: role for role in response.json()}


def _create_user_with_role(
    client: TestClient, headers: dict[str, str], username: str, role_id: int
) -> dict:
    response = client.post(
        "/api/users",
        headers=headers,
        json={
            "username": username,
            "password": "Passw0rd-123",
            "role_ids": [role_id],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_role_crud_and_unique_code(client, auth_headers) -> None:
    roles = _roles(client, auth_headers)
    assert set(roles) == {"admin", "operator", "viewer"}
    assert roles["admin"]["is_system"] is True
    assert roles["viewer"]["is_system"] is False

    created = client.post(
        "/api/roles",
        headers=auth_headers,
        json={"code": "auditor", "name": "审计员", "description": "只读审计"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["code"] == "auditor"
    assert body["description"] == "只读审计"
    assert body["is_active"] is True
    assert body["is_system"] is False

    duplicate = client.post(
        "/api/roles", headers=auth_headers, json={"code": "auditor", "name": "重复"}
    )
    assert duplicate.status_code == 409

    # code 规则：小写字母开头，2..50 位。
    assert (
        client.post(
            "/api/roles", headers=auth_headers, json={"code": "Auditor", "name": "x"}
        ).status_code
        == 422
    )
    assert (
        client.post("/api/roles", headers=auth_headers, json={"code": "a", "name": "x"}).status_code
        == 422
    )

    updated = client.put(
        f"/api/roles/{body['id']}",
        headers=auth_headers,
        json={"name": "高级审计员", "description": "更新后的描述", "is_active": False},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "高级审计员"
    assert updated.json()["description"] == "更新后的描述"
    assert updated.json()["is_active"] is False
    assert updated.json()["code"] == "auditor"

    assert client.delete(f"/api/roles/{body['id']}", headers=auth_headers).status_code == 204
    assert "auditor" not in _roles(client, auth_headers)


def test_admin_role_is_system_and_locked(client, auth_headers) -> None:
    admin = _roles(client, auth_headers)["admin"]

    assert client.delete(f"/api/roles/{admin['id']}", headers=auth_headers).status_code == 409

    disable = client.put(
        f"/api/roles/{admin['id']}", headers=auth_headers, json={"is_active": False}
    )
    assert disable.status_code == 400

    # name/description 仍允许修改。
    renamed = client.put(
        f"/api/roles/{admin['id']}", headers=auth_headers, json={"name": "超级管理员"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "超级管理员"
    assert renamed.json()["is_system"] is True

    blocked = client.put(
        f"/api/roles/{admin['id']}/menus",
        headers=auth_headers,
        json={"menu_ids": []},
    )
    assert blocked.status_code == 409

    # admin 的 GET 返回全部菜单 id，而不是空的 role_menus。
    all_menu_ids = {
        menu["id"]
        for menu in client.get("/api/menus", headers=auth_headers, params={"limit": 500}).json()
    }
    granted = client.get(f"/api/roles/{admin['id']}/menus", headers=auth_headers)
    assert granted.status_code == 200
    assert set(granted.json()) == all_menu_ids
    assert len(granted.json()) == len(all_menu_ids)


def test_role_with_users_cannot_be_deleted(client, auth_headers) -> None:
    viewer = _roles(client, auth_headers)["viewer"]
    _create_user_with_role(client, auth_headers, "reader", viewer["id"])

    response = client.delete(f"/api/roles/{viewer['id']}", headers=auth_headers)
    assert response.status_code == 409
    assert "1" in response.json()["detail"]


def test_role_menu_assignment_roundtrip(client, auth_headers) -> None:
    operator = _roles(client, auth_headers)["operator"]
    menus = client.get("/api/menus", headers=auth_headers, params={"limit": 500}).json()
    chosen = [menu["id"] for menu in menus[:3]]

    assert operator["is_system"] is False
    update = client.put(
        f"/api/roles/{operator['id']}/menus",
        headers=auth_headers,
        json={"menu_ids": chosen},
    )
    assert update.status_code == 204
    assert update.content == b""

    fetched = client.get(f"/api/roles/{operator['id']}/menus", headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json() == sorted(chosen)

    # 全量替换：重复 id 会去重。
    replaced = client.put(
        f"/api/roles/{operator['id']}/menus",
        headers=auth_headers,
        json={"menu_ids": [chosen[0], chosen[0]]},
    )
    assert replaced.status_code == 204
    assert client.get(f"/api/roles/{operator['id']}/menus", headers=auth_headers).json() == [
        chosen[0]
    ]

    unknown = client.put(
        f"/api/roles/{operator['id']}/menus",
        headers=auth_headers,
        json={"menu_ids": [999999]},
    )
    assert unknown.status_code == 400


def test_role_menus_unknown_role_returns_404(client, auth_headers) -> None:
    assert client.get("/api/roles/999999/menus", headers=auth_headers).status_code == 404
    assert (
        client.put(
            "/api/roles/999999/menus", headers=auth_headers, json={"menu_ids": []}
        ).status_code
        == 404
    )
    assert (
        client.put("/api/roles/999999", headers=auth_headers, json={"name": "x"}).status_code == 404
    )
    assert client.delete("/api/roles/999999", headers=auth_headers).status_code == 404


def test_update_role_rejects_blank_name(client, auth_headers) -> None:
    # RoleCreate 用 min_length=1 卡住空名，Update 的可选语义是「传了就不能是空白」，
    # 否则一个空名字的角色会在用户列表的标签里彻底隐身。
    created = client.post("/api/roles", headers=auth_headers, json={"code": "qa", "name": "质检"})
    assert created.status_code == 201
    role_id = created.json()["id"]

    for blank in ("", "   "):
        response = client.put(f"/api/roles/{role_id}", headers=auth_headers, json={"name": blank})
        assert response.status_code == 422, repr(blank)

    assert _roles(client, auth_headers)["qa"]["name"] == "质检"
