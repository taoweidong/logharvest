from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError

from src.api.dependencies import SessionDep, require_permission
from src.api.v1.schemas import RoleCreate, RoleMenusUpdate, RoleRead, RoleUpdate
from src.application.services.rbac import (
    RbacError,
    create_role,
    delete_role,
    list_roles,
    role_menu_ids,
    set_role_menus,
    update_role,
)
from src.domain.models.auth import User
from src.domain.permissions import Permissions

router = APIRouter(prefix="/roles", tags=["roles"])

RolesReadDep = Annotated[User, Depends(require_permission(Permissions.ROLES_READ))]
RolesWriteDep = Annotated[User, Depends(require_permission(Permissions.ROLES_WRITE))]


def _http_error(exc: RbacError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("", response_model=list[RoleRead])
def get_roles(
    session: SessionDep,
    _: RolesReadDep,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return list_roles(session, limit=limit, offset=offset)


@router.post("", response_model=RoleRead, status_code=201)
def post_role(payload: RoleCreate, session: SessionDep, _: RolesWriteDep):
    try:
        return create_role(session, payload)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="role code already exists") from exc


@router.put("/{role_id}", response_model=RoleRead)
def put_role(role_id: int, payload: RoleUpdate, session: SessionDep, _: RolesWriteDep):
    try:
        return update_role(session, role_id, payload)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.delete("/{role_id}", status_code=204, response_class=Response)
def remove_role(role_id: int, session: SessionDep, _: RolesWriteDep):
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
    session: SessionDep,
    _: RolesWriteDep,
):
    try:
        set_role_menus(session, role_id, payload.menu_ids)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return Response(status_code=204)
