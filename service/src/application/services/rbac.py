"""RBAC 管理服务：用户、角色、菜单的读写与业务约束。

路由层只负责 HTTP 映射，这里集中处理事务与所有跨表规则：

* 用户名 / 角色 code / 菜单 name 的唯一性；
* “最后一名 active admin”的保护（停用、摘除 admin 角色、删除）；
* 菜单树的父子类型约束（目录 -> 目录/页面 -> 权限），以及内置菜单的只读字段。

服务通过 :class:`RbacError` 的子类表达 400/404/409，由路由统一转成
``HTTPException``。唯一性在写库前预检查，路由仍会兜底 ``IntegrityError``。
"""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import case, func, or_
from sqlmodel import Session, select

from src.application.schemas import (
    MenuCreate,
    MenuRead,
    MenuUpdate,
    RoleBriefRead,
    RoleCreate,
    RoleRead,
    RoleUpdate,
    UserCreate,
    UserRead,
    UserUpdate,
)
from src.domain.models.auth import ADMIN_ROLE_CODE, Role, User, UserRole
from src.domain.models.rbac import Menu, RoleMenu
from src.domain.services.passwords import hash_password
from src.domain.text import escape_like

DIRECTORY = 0
PAGE = 1
PERMISSION = 2


class RbacError(Exception):
    """管理接口的业务错误；``status_code`` 由路由映射到 HTTP。"""

    status_code = 400


class RbacNotFoundError(RbacError):
    status_code = 404


class RbacConflictError(RbacError):
    status_code = 409


# --- 用户 -------------------------------------------------------------------


def _username_taken(session: Session, username: str) -> bool:
    return session.exec(select(User.id).where(User.username == username)).first() is not None


def _roles_for_users(session: Session, user_ids: Iterable[int]) -> dict[int, list[RoleBriefRead]]:
    """一次查询取回多用户的角色，避免列表接口 N+1。"""
    ids = [user_id for user_id in user_ids if user_id is not None]
    if not ids:
        return {}
    rows = session.exec(
        select(UserRole.user_id, Role.id, Role.code, Role.name)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id.in_(ids))
        .order_by(Role.id)
    ).all()
    result: dict[int, list[RoleBriefRead]] = {user_id: [] for user_id in ids}
    for user_id, role_id, code, name in rows:
        result[user_id].append(RoleBriefRead(id=role_id, code=code, name=name))
    return result


def _user_read(user: User, roles: list[RoleBriefRead]) -> UserRead:
    return UserRead(
        id=user.id or 0,
        username=user.username,
        nickname=user.nickname,
        is_active=user.is_active,
        created_at=user.created_at,
        roles=roles,
    )


def _user_read_with_roles(session: Session, user: User) -> UserRead:
    roles = _roles_for_users(session, [user.id or 0]).get(user.id or 0, [])
    return _user_read(user, roles)


def _load_roles(session: Session, role_ids: Iterable[int]) -> list[Role]:
    """去重后按 id 取角色；任一 id 不存在都视为 400。"""
    unique_ids = list(dict.fromkeys(role_ids))
    if not unique_ids:
        return []
    roles = list(session.exec(select(Role).where(Role.id.in_(unique_ids))).all())
    by_id = {role.id: role for role in roles}
    missing = [role_id for role_id in unique_ids if role_id not in by_id]
    if missing:
        raise RbacError(f"unknown role ids: {missing}")
    return [by_id[role_id] for role_id in unique_ids]


def list_users(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    keyword: str | None = None,
) -> list[UserRead]:
    statement = select(User).order_by(User.id)
    term = (keyword or "").strip()
    if term:
        pattern = f"%{escape_like(term)}%"
        statement = statement.where(
            or_(
                User.username.ilike(pattern, escape="\\"),
                User.nickname.ilike(pattern, escape="\\"),
            )
        )
    users = list(session.exec(statement.offset(offset).limit(limit)).all())
    roles = _roles_for_users(session, [user.id for user in users if user.id is not None])
    return [_user_read(user, roles.get(user.id or 0, [])) for user in users]


def create_user(session: Session, payload: UserCreate) -> UserRead:
    if _username_taken(session, payload.username):
        raise RbacConflictError("username already exists")
    roles = _load_roles(session, payload.role_ids)

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        nickname=payload.nickname,
        is_active=payload.is_active,
    )
    session.add(user)
    session.flush()
    for role in roles:
        session.add(UserRole(user_id=user.id, role_id=role.id))
    session.commit()
    session.refresh(user)
    return _user_read(user, [RoleBriefRead.model_validate(role) for role in roles])


def update_user(session: Session, *, actor_id: int, user_id: int, payload: UserUpdate) -> UserRead:
    user = session.get(User, user_id)
    if user is None:
        raise RbacNotFoundError("user not found")

    fields = payload.model_dump(exclude_unset=True)
    is_active = fields.get("is_active")
    if is_active is False and user_id == actor_id:
        raise RbacError("cannot disable yourself")
    if is_active is False and _is_last_active_admin(session, user):
        raise RbacConflictError("cannot disable the last active admin")
    nickname = fields.get("nickname")
    if nickname is not None:
        user.nickname = nickname
    if is_active is not None:
        user.is_active = is_active

    session.add(user)
    session.commit()
    session.refresh(user)
    return _user_read_with_roles(session, user)


def _is_last_active_admin(session: Session, user: User) -> bool:
    """目标当前是 active admin 且全库只剩这一名 active admin。"""
    if not user.is_active or user.id is None:
        return False
    admin_role_id = session.exec(select(Role.id).where(Role.code == ADMIN_ROLE_CODE)).first()
    if admin_role_id is None:
        return False
    linked = session.exec(
        select(UserRole.user_id).where(
            UserRole.user_id == user.id, UserRole.role_id == admin_role_id
        )
    ).first()
    if linked is None:
        return False
    active_admin_ids = session.exec(
        select(UserRole.user_id)
        .join(User, User.id == UserRole.user_id)
        .where(UserRole.role_id == admin_role_id, User.is_active.is_(True))
    ).all()
    return len(active_admin_ids) <= 1


def set_user_roles(session: Session, *, user_id: int, role_ids: list[int]) -> UserRead:
    user = session.get(User, user_id)
    if user is None:
        raise RbacNotFoundError("user not found")
    roles = _load_roles(session, role_ids)

    admin_role_id = session.exec(select(Role.id).where(Role.code == ADMIN_ROLE_CODE)).first()
    new_role_ids = {role.id for role in roles}
    currently_admin = (
        admin_role_id is not None
        and session.exec(
            select(UserRole.user_id).where(
                UserRole.user_id == user.id, UserRole.role_id == admin_role_id
            )
        ).first()
        is not None
    )
    if (
        currently_admin
        and admin_role_id not in new_role_ids
        and _is_last_active_admin(session, user)
    ):
        raise RbacConflictError("cannot remove the last active admin")

    existing = session.exec(select(UserRole).where(UserRole.user_id == user.id)).all()
    for link in existing:
        session.delete(link)
    # 先落删除再插入：同一复合主键的 delete/insert 放进同一个 flush 可能撞唯一约束。
    session.flush()
    for role in roles:
        session.add(UserRole(user_id=user.id, role_id=role.id))
    session.commit()
    session.refresh(user)
    return _user_read_with_roles(session, user)


def reset_user_password(session: Session, user_id: int, password: str) -> None:
    user = session.get(User, user_id)
    if user is None:
        raise RbacNotFoundError("user not found")
    user.password_hash = hash_password(password)
    session.add(user)
    session.commit()


def delete_user(session: Session, *, actor_id: int, user_id: int) -> None:
    user = session.get(User, user_id)
    if user is None:
        raise RbacNotFoundError("user not found")
    if user_id == actor_id:
        raise RbacError("cannot delete yourself")
    if _is_last_active_admin(session, user):
        raise RbacConflictError("cannot delete the last active admin")

    links = session.exec(select(UserRole).where(UserRole.user_id == user_id)).all()
    for link in links:
        session.delete(link)
    # PostgreSQL 会立即检查外键；SQLite 默认不检查，所以必须先 flush 关联删除，
    # 否则 SQLAlchemy 的 UoW 可能先发 DELETE users 而撞 fk_user_roles_user_id_users。
    session.flush()
    session.delete(user)
    session.commit()


# --- 角色 -------------------------------------------------------------------


def list_roles(session: Session, *, limit: int = 50, offset: int = 0) -> list[RoleRead]:
    statement = select(Role).order_by(Role.id).offset(offset).limit(limit)
    return [RoleRead.model_validate(role) for role in session.exec(statement).all()]


def create_role(session: Session, payload: RoleCreate) -> RoleRead:
    existing = session.exec(select(Role.id).where(Role.code == payload.code)).first()
    if existing is not None:
        raise RbacConflictError("role code already exists")
    role = Role(**payload.model_dump())
    session.add(role)
    session.commit()
    session.refresh(role)
    return RoleRead.model_validate(role)


def update_role(session: Session, role_id: int, payload: RoleUpdate) -> RoleRead:
    role = session.get(Role, role_id)
    if role is None:
        raise RbacNotFoundError("role not found")

    fields = payload.model_dump(exclude_unset=True)
    if role.is_system and fields.get("is_active") is False:
        raise RbacError("system role cannot be disabled")

    for field in ("name", "description"):
        value = fields.get(field)
        if value is not None:
            setattr(role, field, value)
    is_active = fields.get("is_active")
    if is_active is not None:
        role.is_active = is_active

    session.add(role)
    session.commit()
    session.refresh(role)
    return RoleRead.model_validate(role)


def delete_role(session: Session, role_id: int) -> None:
    role = session.get(Role, role_id)
    if role is None:
        raise RbacNotFoundError("role not found")
    if role.is_system:
        raise RbacConflictError("system role cannot be deleted")

    user_count = int(
        session.exec(
            select(func.count()).select_from(UserRole).where(UserRole.role_id == role_id)
        ).one()
        or 0
    )
    if user_count:
        raise RbacConflictError(f"role is assigned to {user_count} user(s)")

    links = session.exec(select(RoleMenu).where(RoleMenu.role_id == role_id)).all()
    for link in links:
        session.delete(link)
    # 先 flush 关联删除，避免 PostgreSQL 先删角色撞 fk_role_menus_role_id_roles。
    session.flush()
    session.delete(role)
    session.commit()


def role_menu_ids(session: Session, role_id: int) -> list[int]:
    role = session.get(Role, role_id)
    if role is None:
        raise RbacNotFoundError("role not found")
    if role.code == ADMIN_ROLE_CODE:
        # admin 靠通配权限放行，不落 role_menus；前端需要展示“全选”效果。
        ids = session.exec(select(Menu.id).order_by(Menu.id)).all()
        return [int(menu_id) for menu_id in ids if menu_id is not None]
    ids = session.exec(
        select(RoleMenu.menu_id).where(RoleMenu.role_id == role_id).order_by(RoleMenu.menu_id)
    ).all()
    return [int(menu_id) for menu_id in ids if menu_id is not None]


def _load_menus(session: Session, menu_ids: Iterable[int]) -> list[Menu]:
    unique_ids = list(dict.fromkeys(menu_ids))
    if not unique_ids:
        return []
    menus = list(session.exec(select(Menu).where(Menu.id.in_(unique_ids))).all())
    by_id = {menu.id: menu for menu in menus}
    missing = [menu_id for menu_id in unique_ids if menu_id not in by_id]
    if missing:
        raise RbacError(f"unknown menu ids: {missing}")
    return [by_id[menu_id] for menu_id in unique_ids]


def set_role_menus(session: Session, role_id: int, menu_ids: list[int]) -> None:
    role = session.get(Role, role_id)
    if role is None:
        raise RbacNotFoundError("role not found")
    if role.is_system:
        raise RbacConflictError("system role menus cannot be changed")
    menus = _load_menus(session, menu_ids)

    links = session.exec(select(RoleMenu).where(RoleMenu.role_id == role_id)).all()
    for link in links:
        session.delete(link)
    session.flush()
    for menu in menus:
        session.add(RoleMenu(role_id=role_id, menu_id=menu.id))
    session.commit()


# --- 菜单 -------------------------------------------------------------------


def _menu_read(menu: Menu) -> MenuRead:
    return MenuRead.model_validate(menu)


def _flat_menu_statement():
    # 根节点优先，其后按父节点聚簇；rank/id 保证同层及整表顺序稳定。
    root_order = case((Menu.parent_id.is_(None), 0), else_=1)
    return select(Menu).order_by(root_order, Menu.parent_id, Menu.rank, Menu.id)


def list_menus(session: Session, *, limit: int = 50, offset: int = 0) -> list[MenuRead]:
    statement = _flat_menu_statement().offset(offset).limit(limit)
    return [_menu_read(menu) for menu in session.exec(statement).all()]


def menu_tree(session: Session) -> list[MenuRead]:
    menus = list(session.exec(_flat_menu_statement()).all())
    nodes: dict[int, MenuRead] = {
        int(menu.id): _menu_read(menu) for menu in menus if menu.id is not None
    }

    roots: list[MenuRead] = []
    for menu in menus:
        if menu.id is None:
            continue
        node = nodes[int(menu.id)]
        parent = nodes.get(int(menu.parent_id)) if menu.parent_id is not None else None
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)

    def sort_children(node: MenuRead) -> None:
        node.children.sort(key=lambda child: (child.rank, child.id))
        for child in node.children:
            sort_children(child)

    roots.sort(key=lambda node: (node.rank, node.id))
    for root in roots:
        sort_children(root)
    return roots


def _menu_name_taken(session: Session, name: str, *, exclude_id: int | None = None) -> bool:
    statement = select(Menu.id).where(Menu.name == name)
    if exclude_id is not None:
        statement = statement.where(Menu.id != exclude_id)
    return session.exec(statement).first() is not None


def _parent_menu(session: Session, parent_id: int | None) -> Menu | None:
    if parent_id is None:
        return None
    parent = session.get(Menu, parent_id)
    if parent is None:
        raise RbacError("parent menu not found")
    return parent


def _validate_parent_relation(parent: Menu | None, menu_type: int) -> None:
    if parent is None:
        if menu_type != DIRECTORY:
            raise RbacError("root menu must be a directory")
        return
    if parent.menu_type == PERMISSION:
        raise RbacError("permission menu cannot have children")
    if parent.menu_type == DIRECTORY and menu_type not in (DIRECTORY, PAGE):
        raise RbacError("directory children must be directories or pages")
    if parent.menu_type == PAGE and menu_type != PERMISSION:
        raise RbacError("page children must be permission nodes")


def _validate_menu_fields(menu_type: int, path: str, component: str) -> None:
    if menu_type == DIRECTORY:
        if component:
            raise RbacError("directory menu cannot have a component")
        return
    if menu_type == PAGE:
        if not path.strip() or not component.strip():
            raise RbacError("page menu requires path and component")
        return
    if component:
        raise RbacError("permission menu cannot have a component")


def create_menu(session: Session, payload: MenuCreate) -> MenuRead:
    if _menu_name_taken(session, payload.name):
        raise RbacConflictError("menu name already exists")
    parent = _parent_menu(session, payload.parent_id)
    _validate_parent_relation(parent, payload.menu_type)

    path = payload.path
    if payload.menu_type == PERMISSION and not path:
        path = parent.path if parent is not None else ""
    _validate_menu_fields(payload.menu_type, path, payload.component)

    menu = Menu(
        parent_id=payload.parent_id,
        name=payload.name,
        menu_type=payload.menu_type,
        path=path,
        component=payload.component,
        title=payload.title,
        icon=payload.icon,
        rank=payload.rank,
        show_link=payload.show_link,
        keep_alive=payload.keep_alive,
        is_active=payload.is_active,
        is_system=False,
    )
    session.add(menu)
    session.commit()
    session.refresh(menu)
    return _menu_read(menu)


def _is_self_or_descendant(session: Session, menu_id: int, candidate_parent_id: int) -> bool:
    current: int | None = candidate_parent_id
    seen: set[int] = set()
    while current is not None:
        if current == menu_id:
            return True
        if current in seen:  # 数据里出现环时也要拒绝，避免死循环
            return True
        seen.add(current)
        current = session.exec(select(Menu.parent_id).where(Menu.id == current)).first()
    return False


def update_menu(session: Session, menu_id: int, payload: MenuUpdate) -> MenuRead:
    menu = session.get(Menu, menu_id)
    if menu is None:
        raise RbacNotFoundError("menu not found")

    fields: dict[str, Any] = payload.model_dump(exclude_unset=True)

    if menu.is_system:
        # 内置菜单只开放展示类字段；路径/组件/父节点是前端路由契约的一部分。
        for field in ("name", "path", "component"):
            if field in fields and fields[field] != getattr(menu, field):
                raise RbacError(f"built-in menu cannot change {field}")
        if "parent_id" in fields and fields["parent_id"] != menu.parent_id:
            raise RbacError("built-in menu cannot change parent_id")

    name = fields.get("name", menu.name)
    if name != menu.name and _menu_name_taken(session, name, exclude_id=menu_id):
        raise RbacConflictError("menu name already exists")

    parent_id = fields.get("parent_id", menu.parent_id)
    parent_changed = "parent_id" in fields and parent_id != menu.parent_id
    if parent_id is None:
        parent = None
    else:
        parent = session.get(Menu, parent_id)
        if parent is None and parent_changed:
            raise RbacError("parent menu not found")
        # 旧数据的父节点已缺失：按根节点处理，避免 500。
    if parent_changed:
        if parent_id is not None and _is_self_or_descendant(session, menu_id, parent_id):
            raise RbacError("cannot move menu under itself or its descendant")
        _validate_parent_relation(parent, menu.menu_type)

    path = fields.get("path", menu.path)
    if path is None:
        path = ""
    component = fields.get("component", menu.component)
    if component is None:
        component = ""
    if menu.menu_type == PERMISSION and "path" not in fields and parent_changed:
        path = parent.path if parent is not None else ""
    _validate_menu_fields(menu.menu_type, path, component)

    menu.parent_id = parent_id
    menu.name = name
    menu.path = path
    menu.component = component
    for field in ("title", "icon", "rank", "show_link", "keep_alive", "is_active"):
        value = fields.get(field)
        if value is not None:
            setattr(menu, field, value)

    session.add(menu)
    session.commit()
    session.refresh(menu)
    return _menu_read(menu)


def delete_menu(session: Session, menu_id: int) -> None:
    menu = session.get(Menu, menu_id)
    if menu is None:
        raise RbacNotFoundError("menu not found")
    if menu.is_system:
        raise RbacConflictError("built-in menu cannot be deleted")

    has_children = session.exec(select(Menu.id).where(Menu.parent_id == menu_id)).first()
    if has_children is not None:
        raise RbacConflictError("menu still has children")

    links = session.exec(select(RoleMenu).where(RoleMenu.menu_id == menu_id)).all()
    for link in links:
        session.delete(link)
    # 同 delete_role：PostgreSQL 的外键检查要求先把引用行删干净。
    session.flush()
    session.delete(menu)
    session.commit()
