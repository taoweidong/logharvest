"""Build the frontend dynamic route tree from the menu table.

Only directories (``menu_type == 0``) and pages (``menu_type == 1``) are
rendered; permission nodes feed ``meta.auths`` instead.
"""

from collections import defaultdict
from typing import Any

from sqlmodel import Session, select

from src.application.services.auth import (
    accessible_menu_ids,
    all_permission_codes,
    is_admin,
    permission_codes,
)
from src.domain.models.auth import User
from src.domain.models.rbac import Menu
from src.domain.permissions import ALL_PERMISSION_CODE

Route = dict[str, Any]


def _sort_key(menu: Menu) -> tuple[int, int]:
    return menu.rank, menu.id or 0


def _meta(menu: Menu) -> dict[str, Any]:
    return {
        "title": menu.title,
        "icon": menu.icon,
        "rank": menu.rank,
        "showLink": menu.show_link,
        "keepAlive": menu.keep_alive,
    }


def _render(
    menu: Menu,
    children_by_parent: dict[int | None, list[Menu]],
    auths: list[str],
) -> Route | None:
    meta = _meta(menu)
    if menu.menu_type == 1:
        meta["auths"] = list(auths)
        return {
            "path": menu.path,
            "name": menu.name,
            "component": menu.component,
            "meta": meta,
        }

    children: list[Route] = []
    for child in sorted(children_by_parent.get(menu.id, []), key=_sort_key):
        rendered = _render(child, children_by_parent, auths)
        if rendered is not None:
            children.append(rendered)
    if not children:
        # 没有可见子页面的目录不出现在侧边栏，也不注册路由。
        return None
    return {
        "path": menu.path,
        "name": menu.name,
        "component": "",
        "meta": meta,
        "children": children,
    }


def build_user_routes(session: Session, user: User) -> list[Route]:
    """Return the dynamic route list for ``user`` in the frontend envelope shape."""
    if is_admin(session, user):
        menus = session.exec(select(Menu).where(Menu.is_active.is_(True))).all()
        auths = all_permission_codes(session)
    else:
        ids = accessible_menu_ids(session, user)
        menus = (
            session.exec(select(Menu).where(Menu.is_active.is_(True), Menu.id.in_(ids))).all()
            if ids
            else []
        )
        auths = [code for code in permission_codes(session, user) if code != ALL_PERMISSION_CODE]

    visible = [menu for menu in menus if menu.menu_type in (0, 1) and menu.id is not None]
    visible_ids = {menu.id for menu in visible}

    children_by_parent: dict[int | None, list[Menu]] = defaultdict(list)
    for menu in visible:
        if menu.parent_id is None:
            children_by_parent[None].append(menu)
        elif menu.parent_id in visible_ids:
            children_by_parent[menu.parent_id].append(menu)

    routes: list[Route] = []
    for menu in sorted(children_by_parent[None], key=_sort_key):
        rendered = _render(menu, children_by_parent, auths)
        if rendered is not None:
            routes.append(rendered)
    return routes
