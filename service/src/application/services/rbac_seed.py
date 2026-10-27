"""Idempotent RBAC seed: menus, permission nodes and the built-in roles.

种子做两件事：**补缺失**与**重塑内置行**。重塑的边界按字段是否可被管理端编辑分开——

* `parent_id`/`menu_type`/`path`/`component` 是强制字段：`update_menu` 对内置行拒绝这些改动，
  所以按声明重置不可能冲掉任何人工编辑，目录重组靠的就是 `parent_id`；
* `title`/`rank`/`icon` 管理端**可以**改，所以只有当现值仍等于 `legacy` 里记录的旧默认时才更新，
  不相等说明被人定制过，保留现值；不在 `legacy` 里的字段一律不碰；
* `show_link`/`keep_alive`/`is_active` 完全属于用户可编辑，已存在的行永不重置。

用户自建的菜单行只做补缺失，任何字段都不动。`operator`/`viewer` 仍只在首次种子创建；
新增页面的授权用 `ADDED_MENUS_BY_ROLE` 对已存在的角色只补缺失条目，不覆盖既有授权。
"""

from typing import Any

from sqlmodel import Session, select

from src.domain.models.auth import ADMIN_ROLE_CODE, Role
from src.domain.models.rbac import Menu, RoleMenu
from src.domain.permissions import Permissions

DIRECTORY = 0
PAGE = 1
PERMISSION = 2

FORCED_FIELDS = ("menu_type", "path", "component")
GUARDED_FIELDS = ("title", "rank", "icon")

# 任务管理复用既有 LogHarvest 目录行：内置行的 name/path 被保护，新建目录就得删内置行，
# 复用只改 title/icon/rank（守卫式）并把 children 的 parent_id 重指。
DIRECTORIES: tuple[dict[str, Any], ...] = (
    {
        "name": "LogHarvest",
        "menu_type": DIRECTORY,
        "path": "/logharvest",
        "component": "",
        "title": "任务管理",
        "icon": "ep/operation",
        "rank": 1,
        "show_link": True,
        "legacy": {"title": "日志分析", "icon": "ep/search", "rank": 2},
    },
    {
        "name": "RuleCenter",
        "menu_type": DIRECTORY,
        "path": "/rules",
        "component": "",
        "title": "规则管理",
        "icon": "ep/set-up",
        "rank": 2,
        "show_link": True,
    },
    {
        "name": "System",
        "menu_type": DIRECTORY,
        "path": "/system",
        "component": "",
        "title": "系统管理",
        "icon": "ep/setting",
        "rank": 3,
        "show_link": True,
    },
)

# 页面的绝对 path 不随父目录变：normalizeRoutePath 对以 / 开头的 path 原样返回，
# 规则集页挂在 /rules 目录下仍走 /logharvest/rulesets，旧书签不破。
PAGES: tuple[dict[str, Any], ...] = (
    {
        "name": "Jobs",
        "parent": "LogHarvest",
        "menu_type": PAGE,
        "path": "/logharvest/jobs",
        "component": "logharvest/jobs/index",
        "rank": 1,
        "icon": "ep/document",
        "title": "任务创建",
        "legacy": {"title": "分析作业"},
    },
    {
        "name": "JobDetail",
        "parent": "LogHarvest",
        "menu_type": PAGE,
        "path": "/logharvest/jobs/:id",
        "component": "logharvest/jobs/detail",
        "rank": 2,
        "show_link": False,
        "title": "作业详情",
    },
    {
        "name": "TaskStats",
        "parent": "LogHarvest",
        "menu_type": PAGE,
        "path": "/logharvest/stats",
        "component": "logharvest/stats/index",
        "rank": 3,
        "icon": "ep/data-line",
        "title": "任务统计",
    },
    {
        "name": "Matches",
        "parent": "LogHarvest",
        "menu_type": PAGE,
        "path": "/logharvest/matches",
        "component": "logharvest/matches/index",
        "rank": 4,
        "icon": "ep/aim",
        "title": "日志分析",
        "legacy": {"title": "命中结果", "rank": 5},
    },
    {
        "name": "DataSources",
        "parent": "LogHarvest",
        "menu_type": PAGE,
        "path": "/logharvest/datasources",
        "component": "logharvest/datasources/index",
        "rank": 5,
        "icon": "ep/folder-opened",
        "title": "数据源管理",
        "legacy": {"title": "数据源", "rank": 4},
    },
    {
        "name": "Workers",
        "parent": "LogHarvest",
        "menu_type": PAGE,
        "path": "/logharvest/workers",
        "component": "logharvest/workers/index",
        "rank": 6,
        "icon": "ep/cpu",
        "title": "Worker 管理",
    },
    {
        "name": "Overview",
        "parent": "LogHarvest",
        "menu_type": PAGE,
        "path": "/logharvest/overview",
        "component": "logharvest/overview/index",
        "rank": 7,
        "show_link": False,
        "title": "运行概览",
    },
    {
        "name": "RuleMaintenance",
        "parent": "RuleCenter",
        "menu_type": PAGE,
        "path": "/rules/maintenance",
        "component": "rules/maintenance/index",
        "rank": 1,
        "icon": "ep/edit-pen",
        "title": "规则维护",
    },
    {
        "name": "RuleSets",
        "parent": "RuleCenter",
        "menu_type": PAGE,
        "path": "/logharvest/rulesets",
        "component": "logharvest/rulesets/index",
        "rank": 2,
        "icon": "ep/tickets",
        "title": "规则集维护",
        "legacy": {"title": "规则集", "rank": 3},
    },
    {
        "name": "Users",
        "parent": "System",
        "menu_type": PAGE,
        "path": "/system/users",
        "component": "system/users/index",
        "rank": 1,
        "icon": "ep/user",
        "title": "用户管理",
    },
    {
        "name": "Roles",
        "parent": "System",
        "menu_type": PAGE,
        "path": "/system/roles",
        "component": "system/roles/index",
        "rank": 2,
        "icon": "ep/avatar",
        "title": "角色管理",
    },
    {
        "name": "Menus",
        "parent": "System",
        "menu_type": PAGE,
        "path": "/system/menus",
        "component": "system/menus/index",
        "rank": 3,
        "icon": "ep/menu",
        "title": "菜单管理",
    },
    {
        "name": "AuditLogs",
        "parent": "System",
        "menu_type": PAGE,
        "path": "/system/audit-logs",
        "component": "system/audit-logs/index",
        "rank": 4,
        "icon": "ep/memo",
        "title": "操作日志",
    },
)

# 权限节点的 path 继承父页面，rank 在父页面内递增。
PERMISSIONS: tuple[dict[str, Any], ...] = (
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
    {"name": Permissions.AUDIT_READ, "parent": "AuditLogs", "title": "查看操作日志", "rank": 1},
)

OPERATOR_MENUS: tuple[str, ...] = (
    "LogHarvest",
    "Jobs",
    "JobDetail",
    "TaskStats",
    "Matches",
    "DataSources",
    "Workers",
    "Overview",
    "RuleCenter",
    "RuleMaintenance",
    "RuleSets",
    "AuditLogs",
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
    Permissions.AUDIT_READ,
)

VIEWER_MENUS: tuple[str, ...] = (
    "LogHarvest",
    "Jobs",
    "JobDetail",
    "TaskStats",
    "Matches",
    "DataSources",
    "Workers",
    "Overview",
    "RuleCenter",
    "RuleMaintenance",
    "RuleSets",
    Permissions.JOBS_READ,
    Permissions.RULESETS_READ,
    Permissions.DATASOURCES_READ,
    Permissions.MATCHES_READ,
    Permissions.WORKERS_READ,
    Permissions.SYSTEM_READ,
)

# 老库升级时给已存在的默认角色补的新条目；只补缺失，不动它们已有的授权。
# viewer 拿不到 AuditLogs/audit:read：操作日志含"谁改了哪个账号"，只读值守角色不该看到权限拓扑。
ADDED_MENUS_BY_ROLE: dict[str, tuple[str, ...]] = {
    "operator": (
        "TaskStats",
        "RuleCenter",
        "RuleMaintenance",
        "AuditLogs",
        Permissions.AUDIT_READ,
    ),
    "viewer": ("TaskStats", "RuleCenter", "RuleMaintenance"),
}


def _reshape_menu(
    session: Session,
    menu: Menu,
    *,
    parent_id: int | None,
    declared: dict[str, Any],
    legacy: dict[str, Any],
) -> None:
    """把内置行拉回声明形状；可编辑字段只在未被定制过时更新。"""
    changed = False
    if menu.parent_id != parent_id:
        menu.parent_id = parent_id
        changed = True
    for field in FORCED_FIELDS:
        if field in declared and getattr(menu, field) != declared[field]:
            setattr(menu, field, declared[field])
            changed = True
    for field in GUARDED_FIELDS:
        if field not in declared or field not in legacy:
            continue
        if getattr(menu, field) != legacy[field] or getattr(menu, field) == declared[field]:
            continue
        setattr(menu, field, declared[field])
        changed = True
    if changed:
        session.add(menu)


def _ensure_menu(session: Session, existing: dict[str, Menu], declared: dict[str, Any]) -> Menu:
    """Insert a seed menu when absent; re-shape an existing system row."""
    name = str(declared["name"])
    parent_name = declared.get("parent")
    parent = existing.get(str(parent_name)) if parent_name is not None else None
    parent_id = parent.id if parent is not None else None
    columns = {
        key: value for key, value in declared.items() if key not in ("name", "parent", "legacy")
    }

    menu = existing.get(name)
    if menu is None:
        menu = Menu(name=name, parent_id=parent_id, is_system=True, **columns)
        session.add(menu)
        session.flush()
        existing[name] = menu
        return menu

    if menu.is_system:
        legacy = declared.get("legacy") or {}
        _reshape_menu(session, menu, parent_id=parent_id, declared=columns, legacy=legacy)
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
        _grant_menu(session, role, menus, menu_name)
    return role


def _grant_menu(session: Session, role: Role, menus: dict[str, Menu], menu_name: str) -> None:
    menu = menus.get(menu_name)
    if menu is None or menu.id is None:
        return
    granted = session.exec(
        select(RoleMenu).where(RoleMenu.role_id == role.id, RoleMenu.menu_id == menu.id)
    ).first()
    if granted is None:
        session.add(RoleMenu(role_id=role.id, menu_id=menu.id))


def ensure_rbac_seed(session: Session) -> None:
    """Create missing seed menus and built-in roles, re-shape system menus (idempotent).

    operator/viewer 只在首次种子（menus 表为空）时创建：管理员在管理端删除
    默认角色后，重启不应把它悄悄重建回来。admin 是系统保护角色，始终确保存在。
    已存在的默认角色用 ADDED_MENUS_BY_ROLE 补新增页面授权，既有条目不动。
    """
    first_seed = session.exec(select(Menu.id).limit(1)).first() is None
    menus: dict[str, Menu] = {menu.name: menu for menu in session.exec(select(Menu)).all()}

    for item in DIRECTORIES:
        _ensure_menu(session, menus, item)
    for item in PAGES:
        _ensure_menu(session, menus, item)
    for item in PERMISSIONS:
        parent = menus.get(str(item["parent"]))
        if parent is None:
            # 权限节点的父页面在同一个种子运行里先行确保存在；到这里仍缺行说明
            # 库被绕过管理端改坏（system 行被直接 DELETE）。宁可带着可读的报错
            # 起不来，也不能把权限节点错挂成根节点或悄悄漏种。
            raise RuntimeError(
                f"rbac seed: permission '{item['name']}' references missing parent "
                f"page '{item['parent']}'; menus table is corrupt"
            )
        declared = {
            **item,
            "menu_type": PERMISSION,
            "path": parent.path,
            "component": "",
            "show_link": False,
        }
        _ensure_menu(session, menus, declared)

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
        # 旧库的 admin 角色可能由早期脚本创建，没有 is_system 标记。
        admin.is_system = True
        session.add(admin)

    if first_seed:
        _ensure_role(
            session,
            roles,
            menus,
            code="operator",
            name="运维人员",
            description="可创建/操作作业与 Worker，只读规则集和数据源，可查看操作日志",
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
    else:
        for code, menu_names in ADDED_MENUS_BY_ROLE.items():
            role = roles.get(code)
            if role is None:
                continue
            for menu_name in menu_names:
                _grant_menu(session, role, menus, menu_name)

    session.commit()
