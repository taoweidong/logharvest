"""rbac 服务的边界分支：保护规则、环数据、显式 None 字段、空集合。"""

import pytest
from sqlmodel import Session, select

from src.application.schemas import MenuCreate, MenuUpdate, RoleCreate, UserCreate, UserUpdate
from src.application.services import rbac
from src.application.services.auth import ensure_admin
from src.application.services.rbac import (
    RbacConflictError,
    RbacError,
    RbacNotFoundError,
    _is_last_active_admin,
    _load_roles,
    _roles_for_users,
    create_menu,
    create_role,
    create_user,
    delete_role,
    update_menu,
    update_user,
)
from src.application.services.rbac_seed import ensure_rbac_seed
from src.domain.models.rbac import Menu, RoleMenu


def _make_role(session: Session, code: str) -> None:
    create_role(session, RoleCreate(code=code, name=code))


# --- 私有小函数的退化输入 -----------------------------------------------------


def test_roles_for_users_ignores_none_ids(session: Session) -> None:
    assert _roles_for_users(session, [None]) == {}


def test_load_roles_empty_is_noop(session: Session) -> None:
    assert _load_roles(session, []) == []


def test_is_last_active_admin_false_for_inactive_user(session: Session) -> None:
    admin = ensure_admin(session, "admin", "Test-Admin-Pass")
    admin.is_active = False
    session.add(admin)
    session.commit()
    assert _is_last_active_admin(session, admin) is False


# --- 角色删除：先清 role_menus ------------------------------------------------


def test_delete_role_removes_menu_links_but_keeps_system_role(session: Session) -> None:
    ensure_rbac_seed(session)  # admin 是种子保护角色，先种出来再验"不可删"。
    _make_role(session, "temp")
    role = session.exec(select(rbac.Role).where(rbac.Role.code == "temp")).one()
    root = create_menu(session, MenuCreate(name="Root", menu_type=0, path="/r", title="R"))
    page = create_menu(
        session,
        MenuCreate(name="P", parent_id=root.id, menu_type=1, path="/p", component="p", title="P"),
    )
    session.add(RoleMenu(role_id=role.id, menu_id=page.id))
    session.commit()

    delete_role(session, role.id)
    assert session.exec(select(RoleMenu).where(RoleMenu.role_id == role.id)).all() == []

    admin_role = session.exec(select(rbac.Role).where(rbac.Role.code == "admin")).one()
    with pytest.raises(RbacConflictError):
        delete_role(session, admin_role.id)


# --- 菜单：名称冲突 / 环防护 / 显式 None / 权限节点继承路径 -------------------


def test_create_page_menu_without_path_is_rejected(session: Session) -> None:
    parent = create_menu(session, MenuCreate(name="Dir", menu_type=0, path="/dir", title="D"))
    with pytest.raises(RbacError, match="page menu requires path and component"):
        create_menu(
            session,
            MenuCreate(
                name="NoPath",
                parent_id=parent.id,
                menu_type=1,
                path="",
                component="",
                title="N",
            ),
        )


def test_update_menu_name_conflict_detected(session: Session) -> None:
    create_menu(session, MenuCreate(name="Alpha", menu_type=0, path="/alpha", title="A"))
    beta = create_menu(session, MenuCreate(name="Beta", menu_type=0, path="/beta", title="B"))
    with pytest.raises(RbacConflictError, match="menu name already exists"):
        update_menu(session, beta.id, MenuUpdate(name="Alpha"))


def test_update_menu_missing_raises_not_found(session: Session) -> None:
    with pytest.raises(RbacNotFoundError):
        update_menu(session, 99999, MenuUpdate(title="x"))


def test_update_menu_explicit_null_resets_path_and_component(session: Session) -> None:
    """显式传 None 等价"置空"：目录菜单允许空 path/component，页面会被校验拒绝。"""
    directory = create_menu(session, MenuCreate(name="D", menu_type=0, path="/d", title="D"))
    updated = update_menu(session, directory.id, MenuUpdate(path=None, component=None))
    assert updated.path == ""
    assert updated.component == ""

    root = create_menu(session, MenuCreate(name="Root", menu_type=0, path="/r", title="R"))
    page = create_menu(
        session,
        MenuCreate(
            name="Page",
            parent_id=root.id,
            menu_type=1,
            path="/p",
            component="p/index",
            title="P",
        ),
    )
    with pytest.raises(RbacError, match="page menu requires path and component"):
        update_menu(session, page.id, MenuUpdate(path=None))


def test_update_menu_permission_inherits_new_parent_path(session: Session) -> None:
    root = create_menu(session, MenuCreate(name="Root", menu_type=0, path="/r", title="R"))
    p1 = create_menu(
        session,
        MenuCreate(
            name="P1", parent_id=root.id, menu_type=1, path="/p1", component="p1", title="P1"
        ),
    )
    p2 = create_menu(
        session,
        MenuCreate(
            name="P2", parent_id=root.id, menu_type=1, path="/p2", component="p2", title="P2"
        ),
    )
    perm = create_menu(
        session,
        MenuCreate(
            name="perm:read",
            parent_id=p1.id,
            menu_type=2,
            path="/p1",
            title="perm",
        ),
    )
    updated = update_menu(session, perm.id, MenuUpdate(parent_id=p2.id))
    assert updated.parent_id == p2.id
    # 权限节点 path 继承父页面：移动后跟着新父走。
    assert updated.path == "/p2"


def test_update_menu_rejects_cycle_without_hanging(session: Session) -> None:
    a = create_menu(session, MenuCreate(name="A", menu_type=0, path="/a", title="A"))
    b = create_menu(
        session, MenuCreate(name="B", menu_type=0, path="/b", parent_id=a.id, title="B")
    )
    # 直接改库构造 A->B->A 的环（管理端校验正常情况下拦得住，这里测防御分支）。
    orm_a = session.get(Menu, a.id)
    orm_a.parent_id = b.id
    session.add(orm_a)
    session.commit()

    c = create_menu(session, MenuCreate(name="C", menu_type=0, path="/c", title="C"))
    with pytest.raises(RbacError, match="itself or its descendant"):
        update_menu(session, c.id, MenuUpdate(parent_id=b.id))


# --- 用户：最后一名 admin 保护与自操作 ----------------------------------------


def test_update_user_cannot_disable_self(session: Session) -> None:
    admin = ensure_admin(session, "admin", "Test-Admin-Pass")
    with pytest.raises(RbacError, match="cannot disable yourself"):
        update_user(
            session,
            actor_id=admin.id,
            user_id=admin.id,
            payload=UserUpdate(is_active=False),
        )


def test_create_user_rejects_unknown_role_ids(session: Session) -> None:
    with pytest.raises(RbacError, match="unknown role ids"):
        create_user(
            session,
            UserCreate(username="someone", password="Password-123", role_ids=[424242]),
        )
