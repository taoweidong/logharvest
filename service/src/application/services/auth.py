from sqlmodel import Session, select

from src.application.services.rbac_seed import ensure_rbac_seed
from src.domain.models.auth import ADMIN_ROLE_CODE, Role, User, UserRole
from src.domain.models.rbac import Menu, RoleMenu
from src.domain.permissions import ALL_PERMISSION_CODE
from src.domain.services.passwords import hash_password


def ensure_admin(session: Session, username: str, password: str) -> User:
    """Idempotently create the admin user plus its role link."""
    # 先补齐 RBAC 种子：admin 角色由种子统一创建（含 is_system/description），
    # 旧库升级后也能直接拿到完整菜单树。这里不再自己兜底建角色——种子已经保证
    # admin 角色存在，重复一份只会造出缺少 is_system 的第二个定义。
    ensure_rbac_seed(session)

    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        user = User(username=username, password_hash=hash_password(password), nickname=username)
        session.add(user)
        session.commit()
        session.refresh(user)

    role = session.exec(select(Role).where(Role.code == ADMIN_ROLE_CODE)).one()
    link = session.exec(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
    ).first()
    if link is None:
        session.add(UserRole(user_id=user.id, role_id=role.id))
        session.commit()
    return user


def role_codes(session: Session, user: User) -> list[str]:
    """Return the user's active role codes, de-duplicated and sorted."""
    if user.id is None:
        return []
    rows = session.exec(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id, Role.is_active.is_(True))
    ).all()
    return sorted({str(code) for code in rows})


def is_admin(session: Session, user: User) -> bool:
    return ADMIN_ROLE_CODE in role_codes(session, user)


def permission_codes(session: Session, user: User) -> list[str]:
    """Resolve the user's effective permission codes.

    ``admin`` is deliberately not enumerated: it keeps the wildcard so newly
    added permissions are available without re-granting anything.
    """
    if user.id is None:
        return []
    # 复用一次角色查询：这里每请求都会被调用（``require_permission`` 依赖），
    # 再走 ``is_admin`` 会把它变成两条同样的 SQL。
    return permission_codes_from_roles(session, user, role_codes(session, user))


def permission_codes_from_roles(session: Session, user: User, codes: list[str]) -> list[str]:
    """Resolve permission codes from the user's already-fetched role codes.

    调用链里往往刚取过角色（``require_permission``、登录响应、动态路由），
    重查一次就是一条重复 SQL。语义与 :func:`permission_codes` 完全一致。
    """
    if user.id is None:
        return []
    if ADMIN_ROLE_CODE in codes:
        return [ALL_PERMISSION_CODE]
    return _granted_permission_codes(session, user)


def roles_and_permissions(session: Session, user: User) -> tuple[list[str], list[str]]:
    """Role codes and permission codes in one pass over the role query.

    登录响应同时要 ``roles`` 与 ``permissions``；各自独立解析会对角色表查两次，
    这里共享一次结果。语义与分别调用 :func:`role_codes` + :func:`permission_codes`
    完全一致。
    """
    if user.id is None:
        return [], []
    codes = role_codes(session, user)
    return codes, permission_codes_from_roles(session, user, codes)


def _granted_permission_codes(session: Session, user: User) -> list[str]:
    """Non-admin permission codes: active permission nodes granted via active roles."""
    rows = session.exec(
        select(Menu.name)
        .join(RoleMenu, RoleMenu.menu_id == Menu.id)
        .join(Role, Role.id == RoleMenu.role_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user.id,
            Role.is_active.is_(True),
            Menu.is_active.is_(True),
            Menu.menu_type == 2,
        )
    ).all()
    return sorted({str(code) for code in rows})


def all_permission_codes(session: Session) -> list[str]:
    """Every active permission node; used as ``meta.auths`` for admin routes."""
    rows = session.exec(
        select(Menu.name).where(Menu.is_active.is_(True), Menu.menu_type == 2)
    ).all()
    return sorted({str(code) for code in rows})


def accessible_menu_ids(session: Session, user: User, *, admin: bool | None = None) -> set[int]:
    """Menu ids the user may see, with active ancestors filled in.

    Admin sees every active menu (including permission nodes; the navigation
    builder drops ``menu_type == 2``). Normal users start from role grants and
    walk up the parent chain; an inactive ancestor stops the walk so a branch
    cannot surface without its (visible) parent.

    ``admin`` 允许调用方传入已判定的管理员标志：调用链里往往刚做过
    ``is_admin``（比如 ``build_user_routes``），重算一次就是一条重复 SQL。
    """
    if user.id is None:
        return set()

    if admin is None:
        admin = is_admin(session, user)

    active_menus = session.exec(
        select(Menu.id, Menu.parent_id).where(Menu.is_active.is_(True))
    ).all()
    parents: dict[int, int | None] = {
        int(menu_id): parent_id for menu_id, parent_id in active_menus if menu_id is not None
    }
    if admin:
        return set(parents)

    rows = session.exec(
        select(Menu.id)
        .join(RoleMenu, RoleMenu.menu_id == Menu.id)
        .join(Role, Role.id == RoleMenu.role_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user.id,
            Role.is_active.is_(True),
            Menu.is_active.is_(True),
        )
    ).all()

    accessible: set[int] = set()
    for menu_id in rows:
        current = int(menu_id)
        while current in parents and current not in accessible:
            accessible.add(current)
            parent_id = parents[current]
            if parent_id is None:
                break
            current = int(parent_id)
    return accessible
