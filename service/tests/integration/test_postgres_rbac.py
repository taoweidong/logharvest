"""PostgreSQL RBAC 删除路径回归。

SQLite 默认不启用外键，`DELETE users` 先于 `DELETE user_roles` 也会“通过”；
PostgreSQL 会立即报 FK violation。这里用真实库钉住三处删除的 flush 顺序：

* 用户删除：user_roles 引用 users；
* 角色删除：role_menus 引用 roles；
* 菜单删除：role_menus 引用 menus。
"""

import os

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from src.application.services.rbac import delete_menu, delete_role, delete_user
from src.domain.models.auth import Role, User, UserRole
from src.domain.models.rbac import Menu, RoleMenu
from src.domain.services.passwords import hash_password
from src.infrastructure.database import create_db_and_tables, create_engine_from_settings

PG_URL = os.getenv("LOGHARVEST_TEST_PG_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="LOGHARVEST_TEST_PG_URL 未设置：跳过 PostgreSQL RBAC 集成测试"
)


@pytest.fixture()
def engine():
    admin = create_engine_from_settings(PG_URL)
    with admin.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    create_db_and_tables(admin)
    admin.dispose()
    fresh = create_engine_from_settings(PG_URL)
    try:
        yield fresh
    finally:
        fresh.dispose()


def _role(session: Session, code: str = "viewer") -> Role:
    role = Role(code=code, name=code)
    session.add(role)
    session.flush()
    return role


def _menu(session: Session, name: str = "custom:read") -> Menu:
    menu = Menu(name=name, menu_type=2, title=name, is_system=False)
    session.add(menu)
    session.flush()
    return menu


def test_delete_user_flushes_user_role_links_first(engine) -> None:
    with Session(engine) as session:
        role = _role(session)
        user = User(
            username="delete-me",
            password_hash=hash_password("Delete-Me-Pass-1"),
        )
        session.add(user)
        session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        session.commit()
        user_id, role_id = user.id, role.id

        delete_user(session, actor_id=999, user_id=user_id)

        assert session.get(User, user_id) is None
        assert session.exec(select(UserRole).where(UserRole.user_id == user_id)).all() == []
        # 只删用户，不动角色。
        assert session.get(Role, role_id) is not None


def test_delete_role_flushes_role_menu_links_first(engine) -> None:
    with Session(engine) as session:
        role = _role(session)
        menu = _menu(session)
        session.add(RoleMenu(role_id=role.id, menu_id=menu.id))
        session.commit()
        role_id, menu_id = role.id, menu.id

        delete_role(session, role_id)

        assert session.get(Role, role_id) is None
        assert session.exec(select(RoleMenu).where(RoleMenu.role_id == role_id)).all() == []
        assert session.get(Menu, menu_id) is not None


def test_delete_menu_flushes_role_menu_links_first(engine) -> None:
    with Session(engine) as session:
        role = _role(session)
        menu = _menu(session)
        session.add(RoleMenu(role_id=role.id, menu_id=menu.id))
        session.commit()
        role_id, menu_id = role.id, menu.id

        delete_menu(session, menu_id)

        assert session.get(Menu, menu_id) is None
        assert session.exec(select(RoleMenu).where(RoleMenu.menu_id == menu_id)).all() == []
        assert session.get(Role, role_id) is not None
