from sqlmodel import Session, select

from src.application.services.rbac_seed import ensure_rbac_seed
from src.domain.models.auth import Role, User
from src.domain.models.rbac import Menu, RoleMenu
from src.domain.permissions import all_permission_codes
from src.domain.services.passwords import hash_password

VIEWER_PASSWORD = "Viewer-Pass-123"
OPERATOR_PASSWORD = "Operator-Pass-123"
NO_ROLE_PASSWORD = "No-Role-Pass-1"

# 2 个目录 + 10 个页面 + 18 个权限节点。
SEED_MENU_COUNT = 30


def _count(session: Session, model: type) -> int:
    return len(session.exec(select(model)).all())


def test_rbac_seed_is_idempotent(session: Session) -> None:
    ensure_rbac_seed(session)
    first = (
        _count(session, Menu),
        _count(session, Role),
        _count(session, RoleMenu),
    )

    ensure_rbac_seed(session)
    second = (
        _count(session, Menu),
        _count(session, Role),
        _count(session, RoleMenu),
    )

    assert first == second
    assert first[0] == SEED_MENU_COUNT
    assert {role.code for role in session.exec(select(Role)).all()} == {
        "admin",
        "operator",
        "viewer",
    }

    # admin 靠权限解析特判拿全部权限，不落 role_menus。
    admin = session.exec(select(Role).where(Role.code == "admin")).one()
    assert admin.is_system is True
    assert session.exec(select(RoleMenu).where(RoleMenu.role_id == admin.id)).all() == []


def test_rbac_seed_preserves_existing_menu_edits(session: Session) -> None:
    ensure_rbac_seed(session)
    jobs = session.exec(select(Menu).where(Menu.name == "Jobs")).one()
    jobs.title = "自定义作业标题"
    jobs.rank = 99
    session.add(jobs)
    session.commit()

    ensure_rbac_seed(session)

    session.expire_all()
    jobs = session.exec(select(Menu).where(Menu.name == "Jobs")).one()
    assert jobs.title == "自定义作业标题"
    assert jobs.rank == 99


def test_deleted_default_role_is_not_recreated_on_restart(session: Session) -> None:
    """operator/viewer 只在首次种子创建；管理端删除后重启不能悄悄重建。"""
    ensure_rbac_seed(session)
    viewer = session.exec(select(Role).where(Role.code == "viewer")).one()
    links = session.exec(select(RoleMenu).where(RoleMenu.role_id == viewer.id)).all()
    for link in links:
        session.delete(link)
    session.delete(viewer)
    session.commit()

    ensure_rbac_seed(session)

    assert session.exec(select(Role).where(Role.code == "viewer")).first() is None
    # admin 仍然在；菜单也没有被重复插入。
    assert session.exec(select(Role).where(Role.code == "admin")).first() is not None
    assert _count(session, Menu) == SEED_MENU_COUNT


def test_viewer_login_returns_read_only_permissions(client, viewer_user) -> None:
    response = client.post("/api/login", json={"username": "viewer", "password": VIEWER_PASSWORD})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["roles"] == ["viewer"]
    assert "jobs:read" in data["permissions"]
    assert "jobs:write" not in data["permissions"]
    assert "roles:write" not in data["permissions"]


def test_viewer_routes_hide_system_and_admin_sees_it(client, viewer_headers, auth_headers) -> None:
    viewer_data = client.get("/api/auth/routes", headers=viewer_headers).json()["data"]
    viewer_paths = {route["path"] for route in viewer_data}
    assert "/logharvest" in viewer_paths
    assert "/system" not in viewer_paths

    logharvest = next(route for route in viewer_data if route["path"] == "/logharvest")
    page_routes = [child for child in logharvest["children"] if child["component"]]
    page_paths = {route["path"] for route in page_routes}
    assert {"/logharvest/jobs", "/logharvest/jobs/:id"} <= page_paths
    assert all("jobs:read" in route["meta"]["auths"] for route in page_routes)

    admin_data = client.get("/api/auth/routes", headers=auth_headers).json()["data"]
    admin_paths = {route["path"] for route in admin_data}
    assert {"/logharvest", "/system"} <= admin_paths
    system = next(route for route in admin_data if route["path"] == "/system")
    assert {child["path"] for child in system["children"]} == {
        "/system/users",
        "/system/roles",
        "/system/menus",
    }


def test_operator_login_can_write_jobs(client, operator_user) -> None:
    response = client.post(
        "/api/login", json={"username": "operator", "password": OPERATOR_PASSWORD}
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["roles"] == ["operator"]
    assert {"jobs:read", "jobs:write", "jobs:operate"} <= set(data["permissions"])
    assert "users:read" not in data["permissions"]


def test_every_declared_permission_code_is_seeded(session: Session) -> None:
    """路由声明的权限码必须在种子里存在，否则该接口对所有人都是 403。

    权限码写错没有任何编译期报错，只能靠这条用例在加权限时立刻暴露。
    """
    ensure_rbac_seed(session)
    seeded = {menu.name for menu in session.exec(select(Menu).where(Menu.menu_type == 2)).all()}
    assert set(all_permission_codes()) <= seeded


def test_user_without_roles_gets_no_permissions_or_routes(client, session: Session) -> None:
    session.add(
        User(
            username="no-role",
            password_hash=hash_password(NO_ROLE_PASSWORD),
            nickname="no-role",
        )
    )
    session.commit()

    login = client.post("/api/login", json={"username": "no-role", "password": NO_ROLE_PASSWORD})
    assert login.status_code == 200
    data = login.json()["data"]
    assert data["roles"] == []
    assert data["permissions"] == []

    headers = {"Authorization": f"Bearer {data['accessToken']}"}
    routes = client.get("/api/auth/routes", headers=headers)
    assert routes.status_code == 200
    assert routes.json()["data"] == []
