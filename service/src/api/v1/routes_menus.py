from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import IntegrityError

from src.api.dependencies import SessionDep, require_permission
from src.api.v1.helpers import client_ip
from src.application.schemas import MenuCreate, MenuRead, MenuUpdate
from src.application.services.audit import commit_operation, record_operation
from src.application.services.rbac import (
    RbacError,
    create_menu,
    delete_menu,
    list_menus,
    menu_tree,
    update_menu,
)
from src.domain.models.auth import User
from src.domain.models.rbac import Menu
from src.domain.permissions import Permissions

router = APIRouter(prefix="/menus", tags=["menus"])

MenusReadDep = Annotated[User, Depends(require_permission(Permissions.MENUS_READ))]
MenusWriteDep = Annotated[User, Depends(require_permission(Permissions.MENUS_WRITE))]

# 审计 detail 只取管理端可改的展示/结构字段；menu_type 不可改，快照里没有它。
_SNAPSHOT_FIELDS = (
    "name",
    "parent_id",
    "path",
    "component",
    "title",
    "icon",
    "rank",
    "show_link",
    "keep_alive",
    "is_active",
)


def _http_error(exc: RbacError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _menu_snapshot(session: SessionDep, menu_id: int, *, only: set[str] | None = None) -> dict:
    """变更前字段值；``only`` 用来把 detail 收窄到真正被改的那几个字段。"""
    menu = session.get(Menu, menu_id)
    if menu is None:
        return {}
    fields = [field for field in _SNAPSHOT_FIELDS if only is None or field in only]
    return {field: getattr(menu, field) for field in fields}


@router.get("", response_model=list[MenuRead])
def get_menus(
    session: SessionDep,
    _: MenusReadDep,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return list_menus(session, limit=limit, offset=offset)


@router.get("/tree", response_model=list[MenuRead])
def get_menu_tree(session: SessionDep, _: MenusReadDep):
    return menu_tree(session)


@router.post("", response_model=MenuRead, status_code=201)
def post_menu(
    payload: MenuCreate,
    request: Request,
    session: SessionDep,
    actor: MenusWriteDep,
):
    try:
        created = create_menu(session, payload)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="menu name already exists") from exc

    # 新菜单 id 只有业务提交后才存在，审计只能随后单独收尾。
    record_operation(
        session,
        actor=actor,
        action="menu.create",
        resource_type="menu",
        resource_id=created.id,
        summary=f"创建菜单 {created.name}",
        detail={"after": _menu_snapshot(session, created.id)},
        ip=client_ip(request),
    )
    commit_operation(session)
    return created


@router.put("/{menu_id}", response_model=MenuRead)
def put_menu(
    menu_id: int,
    payload: MenuUpdate,
    request: Request,
    session: SessionDep,
    actor: MenusWriteDep,
):
    fields = payload.model_dump(exclude_unset=True)
    # 快照只取被改的字段，外加 name：日志行要能认出"改的是哪个菜单"。
    before = _menu_snapshot(session, menu_id, only=set(fields) | {"name"})
    record_operation(
        session,
        actor=actor,
        action="menu.update",
        resource_type="menu",
        resource_id=menu_id,
        summary=f"更新菜单 {before.get('name') or menu_id}",
        detail={"before": before, "after": fields},
        ip=client_ip(request),
    )
    try:
        return update_menu(session, menu_id, payload)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.delete("/{menu_id}", status_code=204, response_class=Response)
def remove_menu(
    menu_id: int,
    request: Request,
    session: SessionDep,
    actor: MenusWriteDep,
):
    before = _menu_snapshot(session, menu_id)
    record_operation(
        session,
        actor=actor,
        action="menu.delete",
        resource_type="menu",
        resource_id=menu_id,
        summary=f"删除菜单 {before.get('name') or menu_id}",
        detail={"before": before},
        ip=client_ip(request),
    )
    try:
        delete_menu(session, menu_id)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return Response(status_code=204)
