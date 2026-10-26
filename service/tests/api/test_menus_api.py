from fastapi.testclient import TestClient


def _flat(client: TestClient, headers: dict[str, str]) -> list[dict]:
    response = client.get("/api/menus", headers=headers, params={"limit": 500})
    assert response.status_code == 200, response.text
    return response.json()


def _by_name(client: TestClient, headers: dict[str, str]) -> dict[str, dict]:
    return {menu["name"]: menu for menu in _flat(client, headers)}


def _create_menu(client: TestClient, headers: dict[str, str], **payload) -> dict:
    response = client.post("/api/menus", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _directory(client: TestClient, headers: dict[str, str], name: str) -> dict:
    return _create_menu(
        client,
        headers,
        name=name,
        menu_type=0,
        title=name,
        path=f"/{name.lower()}",
    )


def _page(client: TestClient, headers: dict[str, str], name: str, parent_id: int) -> dict:
    return _create_menu(
        client,
        headers,
        name=name,
        menu_type=1,
        parent_id=parent_id,
        title=name,
        path=f"/{name.lower()}",
        component=f"custom/{name.lower()}/index",
    )


def _permission(client: TestClient, headers: dict[str, str], name: str, parent_id: int) -> dict:
    return _create_menu(
        client,
        headers,
        name=name,
        menu_type=2,
        parent_id=parent_id,
        title=name,
    )


def test_menu_tree_contains_seed_branches(client, auth_headers) -> None:
    response = client.get("/api/menus/tree", headers=auth_headers)
    assert response.status_code == 200
    roots = {node["name"]: node for node in response.json()}
    assert {"LogHarvest", "System"} <= set(roots)

    logharvest_children = {child["name"] for child in roots["LogHarvest"]["children"]}
    assert {
        "Jobs",
        "JobDetail",
        "RuleSets",
        "DataSources",
        "Matches",
        "Workers",
        "Overview",
    } <= logharvest_children
    assert {child["name"] for child in roots["System"]["children"]} == {
        "Users",
        "Roles",
        "Menus",
    }

    jobs = next(child for child in roots["LogHarvest"]["children"] if child["name"] == "Jobs")
    assert {child["name"] for child in jobs["children"]} == {
        "jobs:read",
        "jobs:write",
        "jobs:operate",
    }
    # 同级按 rank/id；Jobs 的 rank 为 1。
    assert jobs["children"][0]["rank"] <= jobs["children"][-1]["rank"]


def test_flat_menu_list_is_stable_and_leafless(client, auth_headers) -> None:
    menus = _flat(client, auth_headers)
    assert len(menus) == 30
    assert all(menu["children"] == [] for menu in menus)
    assert len({menu["name"] for menu in menus}) == 30

    page = client.get("/api/menus", headers=auth_headers, params={"limit": 5, "offset": 0})
    assert page.status_code == 200
    assert len(page.json()) == 5


def test_create_custom_directory_page_and_permission(client, auth_headers) -> None:
    directory = _directory(client, auth_headers, "CustomDir")
    assert directory["menu_type"] == 0
    assert directory["is_system"] is False
    assert directory["children"] == []

    page = _page(client, auth_headers, "CustomPage", directory["id"])
    assert page["parent_id"] == directory["id"]
    assert page["component"] == "custom/custompage/index"

    permission = _permission(client, auth_headers, "custom:read", page["id"])
    # 权限节点缺省继承父页面的 path，且没有 component。
    assert permission["path"] == page["path"]
    assert permission["component"] == ""

    tree = client.get("/api/menus/tree", headers=auth_headers).json()
    custom = next(node for node in tree if node["name"] == "CustomDir")
    assert [child["name"] for child in custom["children"]] == ["CustomPage"]
    nested = custom["children"][0]
    assert [child["name"] for child in nested["children"]] == ["custom:read"]


def test_menu_parent_and_type_rules(client, auth_headers) -> None:
    menus = _by_name(client, auth_headers)
    directory_id = menus["LogHarvest"]["id"]
    page_id = menus["Jobs"]["id"]
    permission_id = menus["jobs:read"]["id"]

    def post(payload: dict) -> int:
        return client.post("/api/menus", headers=auth_headers, json=payload).status_code

    # 根节点只能是目录。
    assert (
        post({"name": "RootPage", "menu_type": 1, "title": "x", "path": "/x", "component": "x"})
        == 400
    )
    assert post({"name": "RootPerm", "menu_type": 2, "title": "x"}) == 400

    # 父类型约束。
    assert (
        post({"name": "PermUnderDir", "menu_type": 2, "title": "x", "parent_id": directory_id})
        == 400
    )
    assert (
        post(
            {
                "name": "PageUnderPage",
                "menu_type": 1,
                "title": "x",
                "parent_id": page_id,
                "path": "/x",
                "component": "x",
            }
        )
        == 400
    )
    assert post({"name": "DirUnderPage", "menu_type": 0, "title": "x", "parent_id": page_id}) == 400
    assert (
        post({"name": "PermUnderPerm", "menu_type": 2, "title": "x", "parent_id": permission_id})
        == 400
    )

    # 字段约束。
    assert post({"name": "NoComponentPage", "menu_type": 1, "title": "x", "path": "/x"}) == 400
    assert post({"name": "NoPathPage", "menu_type": 1, "title": "x", "component": "x"}) == 400
    assert post({"name": "DirWithComponent", "menu_type": 0, "title": "x", "component": "x"}) == 400
    assert (
        post(
            {
                "name": "PermWithComponent",
                "menu_type": 2,
                "title": "x",
                "parent_id": page_id,
                "component": "x",
            }
        )
        == 400
    )
    assert post({"name": "MissingParent", "menu_type": 0, "title": "x", "parent_id": 999999}) == 400

    # 名称唯一。
    assert post({"name": "Jobs", "menu_type": 0, "title": "重复"}) == 409


def test_builtin_menu_only_allows_display_fields(client, auth_headers) -> None:
    jobs = _by_name(client, auth_headers)["Jobs"]
    logharvest_id = _by_name(client, auth_headers)["LogHarvest"]["id"]

    rename = client.put(
        f"/api/menus/{jobs['id']}", headers=auth_headers, json={"name": "JobsRenamed"}
    )
    assert rename.status_code == 400

    assert (
        client.put(
            f"/api/menus/{jobs['id']}",
            headers=auth_headers,
            json={"path": "/logharvest/other"},
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"/api/menus/{jobs['id']}",
            headers=auth_headers,
            json={"component": "other/index"},
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"/api/menus/{jobs['id']}",
            headers=auth_headers,
            json={"parent_id": logharvest_id + 1},
        ).status_code
        == 400
    )

    retitled = client.put(
        f"/api/menus/{jobs['id']}",
        headers=auth_headers,
        json={"title": "作业中心", "rank": 9, "show_link": False, "menu_type": 2},
    )
    assert retitled.status_code == 200
    body = retitled.json()
    assert body["title"] == "作业中心"
    assert body["rank"] == 9
    assert body["show_link"] is False
    # menu_type 不可修改：DTO 忽略该字段。
    assert body["menu_type"] == 1

    # 同名提交不算变更。
    same = client.put(f"/api/menus/{jobs['id']}", headers=auth_headers, json={"name": "Jobs"})
    assert same.status_code == 200
    same_parent = client.put(
        f"/api/menus/{jobs['id']}",
        headers=auth_headers,
        json={"parent_id": logharvest_id},
    )
    assert same_parent.status_code == 200


def test_menu_move_rejects_cycles_and_invalid_parent(client, auth_headers) -> None:
    first = _directory(client, auth_headers, "Alpha")
    second = _directory(client, auth_headers, "Beta")

    # 目录挂目录合法。
    moved = client.put(
        f"/api/menus/{second['id']}", headers=auth_headers, json={"parent_id": first["id"]}
    )
    assert moved.status_code == 200
    assert moved.json()["parent_id"] == first["id"]

    # 挂到自己或后代不合法。
    assert (
        client.put(
            f"/api/menus/{first['id']}",
            headers=auth_headers,
            json={"parent_id": second["id"]},
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"/api/menus/{first['id']}", headers=auth_headers, json={"parent_id": first["id"]}
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"/api/menus/{first['id']}", headers=auth_headers, json={"parent_id": 999999}
        ).status_code
        == 400
    )

    # 页面移动到根同样违反“根只能是目录”。
    page = _page(client, auth_headers, "AlphaPage", first["id"])
    assert (
        client.put(
            f"/api/menus/{page['id']}", headers=auth_headers, json={"parent_id": None}
        ).status_code
        == 400
    )
    # 权限节点父节点改为目录：目录不能直接挂权限。
    permission = _permission(client, auth_headers, "alpha:read", page["id"])
    assert (
        client.put(
            f"/api/menus/{permission['id']}",
            headers=auth_headers,
            json={"parent_id": first["id"]},
        ).status_code
        == 400
    )


def test_menu_delete_guards_and_role_menu_cleanup(client, auth_headers) -> None:
    menus = _by_name(client, auth_headers)
    jobs_id = menus["Jobs"]["id"]
    logharvest_id = menus["LogHarvest"]["id"]

    assert client.delete(f"/api/menus/{jobs_id}", headers=auth_headers).status_code == 409
    assert client.delete(f"/api/menus/{logharvest_id}", headers=auth_headers).status_code == 409
    assert client.delete("/api/menus/999999", headers=auth_headers).status_code == 404

    directory = _directory(client, auth_headers, "Tree")
    page = _page(client, auth_headers, "TreePage", directory["id"])
    permission = _permission(client, auth_headers, "tree:read", page["id"])

    assert client.delete(f"/api/menus/{directory['id']}", headers=auth_headers).status_code == 409
    assert client.delete(f"/api/menus/{page['id']}", headers=auth_headers).status_code == 409
    assert client.delete(f"/api/menus/{permission['id']}", headers=auth_headers).status_code == 204

    # 叶子页面被角色授权后删除，role_menus 必须一起清理。
    leaf_dir = _directory(client, auth_headers, "LeafDir")
    leaf = _page(client, auth_headers, "LeafPage", leaf_dir["id"])
    roles = client.get("/api/roles", headers=auth_headers).json()
    operator_id = next(role["id"] for role in roles if role["code"] == "operator")
    assert (
        client.put(
            f"/api/roles/{operator_id}/menus",
            headers=auth_headers,
            json={"menu_ids": [leaf["id"]]},
        ).status_code
        == 204
    )

    assert client.delete(f"/api/menus/{leaf['id']}", headers=auth_headers).status_code == 204
    granted = client.get(f"/api/roles/{operator_id}/menus", headers=auth_headers).json()
    assert leaf["id"] not in granted

    # 子节点清空后父目录可删。
    assert client.delete(f"/api/menus/{page['id']}", headers=auth_headers).status_code == 204
    assert client.delete(f"/api/menus/{leaf_dir['id']}", headers=auth_headers).status_code == 204
