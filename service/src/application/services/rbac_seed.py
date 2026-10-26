"""Idempotent RBAC seed: menus, permission nodes and the built-in roles.

The seed only inserts what is missing. Existing rows (including titles, ranks
and role-menu grants edited in the admin UI) are never overwritten, with the
single exception of forcing ``admin.is_system`` on for databases created
before the RBAC migration.
"""

from sqlmodel import Session, select

from src.domain.models.auth import ADMIN_ROLE_CODE, Role
from src.domain.models.rbac import Menu, RoleMenu
from src.domain.permissions import Permissions

DIRECTORIES: tuple[dict[str, object], ...] = (
    {
        "name": "LogHarvest",
        "path": "/logharvest",
        "title": "日志分析",
        "icon": "ep/search",
        "rank": 2,
    },
    {
        "name": "System",
        "path": "/system",
        "title": "系统管理",
        "icon": "ep/setting",
        "rank": 3,
    },
)

PAGES: tuple[dict[str, object], ...] = (
    {
        "name": "Jobs",
        "parent": "LogHarvest",
        "path": "/logharvest/jobs",
        "component": "logharvest/jobs/index",
        "rank": 1,
        "icon": "ep/document",
        "title": "分析作业",
    },
    {
        "name": "JobDetail",
        "parent": "LogHarvest",
        "path": "/logharvest/jobs/:id",
        "component": "logharvest/jobs/detail",
        "rank": 2,
        "show_link": False,
        "title": "作业详情",
    },
    {
        "name": "RuleSets",
        "parent": "LogHarvest",
        "path": "/logharvest/rulesets",
        "component": "logharvest/rulesets/index",
        "rank": 3,
        "icon": "ep/tickets",
        "title": "规则集",
    },
    {
        "name": "DataSources",
        "parent": "LogHarvest",
        "path": "/logharvest/datasources",
        "component": "logharvest/datasources/index",
        "rank": 4,
        "icon": "ep/folder-opened",
        "title": "数据源",
    },
    {
        "name": "Matches",
        "parent": "LogHarvest",
        "path": "/logharvest/matches",
        "component": "logharvest/matches/index",
        "rank": 5,
        "icon": "ep/aim",
        "title": "命中结果",
    },
    {
        "name": "Workers",
        "parent": "LogHarvest",
        "path": "/logharvest/workers",
        "component": "logharvest/workers/index",
        "rank": 6,
        "icon": "ep/cpu",
        "title": "Worker 管理",
    },
    {
        "name": "Overview",
        "parent": "LogHarvest",
        "path": "/logharvest/overview",
        "component": "logharvest/overview/index",
        "rank": 7,
        "show_link": False,
        "title": "运行概览",
    },
    {
        "name": "Users",
        "parent": "System",
        "path": "/system/users",
        "component": "system/users/index",
        "rank": 1,
        "icon": "ep/user",
        "title": "用户管理",
    },
    {
        "name": "Roles",
        "parent": "System",
        "path": "/system/roles",
        "component": "system/roles/index",
        "rank": 2,
        "icon": "ep/avatar",
        "title": "角色管理",
    },
    {
        "name": "Menus",
        "parent": "System",
        "path": "/system/menus",
        "component": "system/menus/index",
        "rank": 3,
        "icon": "ep/menu",
        "title": "菜单管理",
    },
)

# 权限节点的 path 继承父页面，rank 在父页面内递增。
PERMISSIONS: tuple[dict[str, object], ...] = (
    {"name": Permissions.JOBS_READ, "parent": "Jobs", "title": "查看作业", "rank": 1},
    {"name": Permissions.JOBS_WRITE, "parent": "Jobs", "title": "创建作业", "rank": 2},
    {"name": Permissions.JOBS_OPERATE, "parent": "Jobs", "title": "操作作业", "rank": 3},
    {"name": Permissions.RULESETS_READ, "parent": "RuleSets", "title": "查看规则集", "rank": 1},
    {"name": Permissions.RULESETS_WRITE, "parent": "RuleSets", "title": "维护规则集", "rank": 2},
    {
        "name": Permissions.DATASOURCES_READ,
        "parent": "DataSources",
        "title": "查看数据源",
        "rank": 1,
    },
    {
        "name": Permissions.DATASOURCES_WRITE,
        "parent": "DataSources",
        "title": "维护数据源",
        "rank": 2,
    },
    {"name": Permissions.MATCHES_READ, "parent": "Matches", "title": "查看命中结果", "rank": 1},
    {"name": Permissions.WORKERS_READ, "parent": "Workers", "title": "查看 Worker", "rank": 1},
    {
        "name": Permissions.WORKERS_OPERATE,
        "parent": "Workers",
        "title": "操作 Worker",
        "rank": 2,
    },
    {"name": Permissions.SYSTEM_READ, "parent": "Overview", "title": "查看系统状态", "rank": 1},
    {
        "name": Permissions.SYSTEM_OPERATE,
        "parent": "Overview",
        "title": "执行系统维护",
        "rank": 2,
    },
    {"name": Permissions.USERS_READ, "parent": "Users", "title": "查看用户", "rank": 1},
    {"name": Permissions.USERS_WRITE, "parent": "Users", "title": "维护用户", "rank": 2},
    {"name": Permissions.ROLES_READ, "parent": "Roles", "title": "查看角色", "rank": 1},
    {"name": Permissions.ROLES_WRITE, "parent": "Roles", "title": "维护角色", "rank": 2},
    {"name": Permissions.MENUS_READ, "parent": "Menus", "title": "查看菜单", "rank": 1},
    {"name": Permissions.MENUS_WRITE, "parent": "Menus", "title": "维护菜单", "rank": 2},
)

OPERATOR_MENUS: tuple[str, ...] = (
    "LogHarvest",
    "Jobs",
    "JobDetail",
    "RuleSets",
    "DataSources",
    "Matches",
    "Workers",
    "Overview",
    Permissions.JOBS_READ,
    Permissions.JOBS_WRITE,
    Permissions.JOBS_OPERATE,
    Permissions.RULESETS_READ,
    Permissions.DATASOURCES_READ,
    Permissions.MATCHES_READ,
    Permissions.WORKERS_READ,
    Permissions.WORKERS_OPERATE,
    Permissions.SYSTEM_READ,
    Permissions.SYSTEM_OPERATE,
)

VIEWER_MENUS: tuple[str, ...] = (
    "LogHarvest",
    "Jobs",
    "JobDetail",
    "RuleSets",
    "DataSources",
    "Matches",
    "Workers",
    "Overview",
    Permissions.JOBS_READ,
    Permissions.RULESETS_READ,
    Permissions.DATASOURCES_READ,
    Permissions.MATCHES_READ,
    Permissions.WORKERS_READ,
    Permissions.SYSTEM_READ,
)


def _ensure_menu(
    session: Session,
    existing: dict[str, Menu],
    *,
    name: str,
    parent_name: str | None = None,
    **fields: object,
) -> Menu:
    """Insert a seed menu when absent; return the existing row untouched otherwise."""
    menu = existing.get(name)
    if menu is not None:
        return menu

    parent = existing.get(parent_name) if parent_name is not None else None
    menu = Menu(
        name=name,
        parent_id=parent.id if parent is not None else None,
        is_system=True,
        **fields,
    )
    session.add(menu)
    session.flush()
    existing[name] = menu
    return menu


def _ensure_role(
    session: Session,
    existing: dict[str, Role],
    menus: dict[str, Menu],
    *,
    code: str,
    name: str,
    description: str,
    menu_names: tuple[str, ...] = (),
) -> Role:
    """Insert a built-in role and its initial grants; never touch an existing role."""
    role = existing.get(code)
    if role is not None:
        return role

    role = Role(code=code, name=name, description=description)
    session.add(role)
    session.flush()
    existing[code] = role

    for menu_name in menu_names:
        menu = menus.get(menu_name)
        if menu is not None and menu.id is not None:
            session.add(RoleMenu(role_id=role.id, menu_id=menu.id))
    return role


def ensure_rbac_seed(session: Session) -> None:
    """Create missing seed menus and built-in roles (idempotent).

    operator/viewer 只在首次种子（menus 表为空）时创建：管理员在管理端删除
    默认角色后，重启不应把它悄悄重建回来。admin 是系统保护角色，始终确保存在。
    """
    first_seed = session.exec(select(Menu.id).limit(1)).first() is None
    menus: dict[str, Menu] = {menu.name: menu for menu in session.exec(select(Menu)).all()}

    for item in DIRECTORIES:
        _ensure_menu(session, menus, menu_type=0, **item)
    for item in PAGES:
        fields = dict(item)
        parent_name = str(fields.pop("parent"))
        _ensure_menu(session, menus, parent_name=parent_name, menu_type=1, **fields)
    for item in PERMISSIONS:
        fields = dict(item)
        parent_name = str(fields.pop("parent"))
        parent = menus[parent_name]
        _ensure_menu(
            session,
            menus,
            name=str(fields.pop("name")),
            parent_name=parent_name,
            menu_type=2,
            path=parent.path,
            show_link=False,
            **fields,
        )

    roles: dict[str, Role] = {role.code: role for role in session.exec(select(Role)).all()}
    admin = roles.get(ADMIN_ROLE_CODE)
    if admin is None:
        admin = Role(
            code=ADMIN_ROLE_CODE,
            name="管理员",
            description="系统管理员，拥有全部权限",
            is_system=True,
        )
        session.add(admin)
        session.flush()
        roles[ADMIN_ROLE_CODE] = admin
    elif not admin.is_system:
        # 旧库的 admin 角色由 ensure_admin 创建，没有 is_system 标记。
        admin.is_system = True

    if first_seed:
        _ensure_role(
            session,
            roles,
            menus,
            code="operator",
            name="运维人员",
            description="可创建/操作作业与 Worker，只读规则集和数据源",
            menu_names=OPERATOR_MENUS,
        )
        _ensure_role(
            session,
            roles,
            menus,
            code="viewer",
            name="只读用户",
            description="只读查看作业、规则集、数据源、命中与 Worker",
            menu_names=VIEWER_MENUS,
        )

    session.commit()
