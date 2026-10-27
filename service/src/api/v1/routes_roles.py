from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from src.api.dependencies import SessionDep, require_permission
from src.api.v1.helpers import client_ip
from src.application.schemas import RoleCreate, RoleMenusUpdate, RoleRead, RoleUpdate
from src.application.services.audit import commit_operation, record_operation
from src.application.services.rbac import (
    RbacError,
    create_role,
    delete_role,
    list_roles,
    role_menu_ids,
    set_role_menus,
    update_role,
)
from src.domain.models.auth import Role, User
from src.domain.models.rbac import RoleMenu
from src.domain.permissions import Permissions

router = APIRouter(prefix="/roles", tags=["roles"])

RolesReadDep = Annotated[User, Depends(require_permission(Permissions.ROLES_READ))]
RolesWriteDep = Annotated[User, Depends(require_permission(Permissions.ROLES_WRITE))]


def _http_error(exc: RbacError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _role_snapshot(session: SessionDep, role_id: int) -> dict[str, object]:
    role = session.get(Role, role_id)
    if role is None:
        return {}
    return {
        "code": role.code,
        "name": role.name,
        "description": role.description,
        "is_active": role.is_active,
    }


def _granted_menu_ids(session: SessionDep, role_id: int) -> list[int]:
    rows = session.exec(select(RoleMenu.menu_id).where(RoleMenu.role_id == role_id)).all()
    return sorted(int(menu_id) for menu_id in rows)


@router.get("", response_model=list[RoleRead])
def get_roles(
    session: SessionDep,
    _: RolesReadDep,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return list_roles(session, limit=limit, offset=offset)


@router.post("", response_model=RoleRead, status_code=201)
def post_role(
    payload: RoleCreate,
    request: Request,
    session: SessionDep,
    actor: RolesWriteDep,
):
    try:
        created = create_role(session, payload)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="role code already exists") from exc

    # 新角色 id 只有业务提交后才存在，审计只能随后单独收尾。
    record_operation(
        session,
        actor=actor,
        action="role.create",
        resource_type="role",
        resource_id=created.id,
        summary=f"创建角色 {created.code}",
        detail={
            "after": {
                "code": created.code,
                "name": created.name,
                "is_active": created.is_active,
            }
        },
        ip=client_ip(request),
    )
    commit_operation(session)
    return created


@router.put("/{role_id}", response_model=RoleRead)
def put_role(
    role_id: int,
    payload: RoleUpdate,
    request: Request,
    session: SessionDep,
    actor: RolesWriteDep,
):
    before = _role_snapshot(session, role_id)
    record_operation(
        session,
        actor=actor,
        action="role.update",
        resource_type="role",
        resource_id=role_id,
        summary=f"更新角色 {before.get('code') or role_id}",
        detail={"before": before, "after": payload.model_dump(exclude_unset=True)},
        ip=client_ip(request),
    )
    try:
        return update_role(session, role_id, payload)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.delete("/{role_id}", status_code=204, response_class=Response)
def remove_role(
    role_id: int,
    request: Request,
    session: SessionDep,
    actor: RolesWriteDep,
):
    before = _role_snapshot(session, role_id)
    record_operation(
        session,
        actor=actor,
        action="role.delete",
        resource_type="role",
        resource_id=role_id,
        summary=f"删除角色 {before.get('code') or role_id}",
        detail={"before": before},
        ip=client_ip(request),
    )
    try:
        delete_role(session, role_id)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return Response(status_code=204)


@router.get("/{role_id}/menus", response_model=list[int])
def get_role_menus(role_id: int, session: SessionDep, _: RolesReadDep):
    try:
        return role_menu_ids(session, role_id)
    except RbacError as exc:
        raise _http_error(exc) from exc


@router.put("/{role_id}/menus", status_code=204, response_class=Response)
def put_role_menus(
    role_id: int,
    payload: RoleMenusUpdate,
    request: Request,
    session: SessionDep,
    actor: RolesWriteDep,
):
    before = _granted_menu_ids(session, role_id)
    after = sorted(set(payload.menu_ids))
    record_operation(
        session,
        actor=actor,
        action="role.assign_menus",
        resource_type="role",
        resource_id=role_id,
        summary=f"调整角色 {role_id} 的菜单授权",
        # 数量与增量是排查越权的第一手线索：只存集合大小和差集，不存菜单内容。
        detail={
            "before_count": len(before),
            "after_count": len(after),
            "added": sorted(set(after) - set(before)),
            "removed": sorted(set(before) - set(after)),
        },
        ip=client_ip(request),
    )
    try:
        set_role_menus(session, role_id, payload.menu_ids)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return Response(status_code=204)
