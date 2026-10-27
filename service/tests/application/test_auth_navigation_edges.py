"""auth 服务与 navigation 的退化分支：无 id 的 User、admin 快路径、空目录。

这些分支在正常业务里几乎不可达（User 一定落库后有 id），但它们是权限解析
的"宁可从严"守卫——退化输入必须得到明确的空结果而不是异常。
"""

from sqlmodel import Session

from src.application.services.auth import (
    accessible_menu_ids,
    all_permission_codes,
    ensure_admin,
    is_admin,
    permission_codes,
    role_codes,
    roles_and_permissions,
)
from src.application.services.navigation import build_user_routes
from src.domain.models.auth import User


def _ghost() -> User:
    return User(username="ghost", password_hash="x")  # 未落库：id is None


def test_role_codes_for_user_without_id_is_empty(session: Session) -> None:
    assert role_codes(session, _ghost()) == []


def test_is_admin_for_user_without_id_is_false(session: Session) -> None:
    assert is_admin(session, _ghost()) is False


def test_permission_codes_for_user_without_id_is_empty(session: Session) -> None:
    assert permission_codes(session, _ghost()) == []


def test_roles_and_permissions_for_user_without_id_is_empty(session: Session) -> None:
    assert roles_and_permissions(session, _ghost()) == ([], [])


def test_accessible_menu_ids_for_user_without_id_is_empty(session: Session) -> None:
    assert accessible_menu_ids(session, _ghost()) == set()


def test_roles_and_permissions_admin_gets_wildcard(session: Session) -> None:
    admin = ensure_admin(session, "admin", "Test-Admin-Pass")
    roles, permissions = roles_and_permissions(session, admin)
    assert roles == ["admin"]
    assert permissions == ["*:*:*"]


def test_build_user_routes_admin_sees_every_active_menu(session: Session) -> None:
    admin = ensure_admin(session, "admin", "Test-Admin-Pass")
    routes = build_user_routes(session, admin)
    # 种子有三个顶级目录（LogHarvest/RuleCenter/System），全部 active。
    assert len(routes) == 3
    assert all_permission_codes(session)  # admin 的 meta.auths 是全量权限码


def test_build_user_routes_empty_directory_is_dropped(session: Session) -> None:
    """没有可见子页面的目录不进路由（navigation._render 的 None 分支）。"""
    admin = ensure_admin(session, "admin", "Test-Admin-Pass")
    from sqlmodel import select

    from src.domain.models.rbac import Menu

    empty_dir = Menu(name="EmptyDir", menu_type=0, path="/empty", title="空目录", is_active=True)
    session.add(empty_dir)
    session.commit()

    routes = build_user_routes(session, admin)
    assert all(route["name"] != "EmptyDir" for route in routes)
    assert session.exec(select(Menu).where(Menu.name == "EmptyDir")).one() is not None
