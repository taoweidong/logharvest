from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError

from src.api.dependencies import SessionDep, require_permission
from src.api.v1.schemas import (
    PasswordReset,
    UserCreate,
    UserRead,
    UserRolesUpdate,
    UserUpdate,
)
from src.application.services.rbac import (
    RbacError,
    create_user,
    delete_user,
    list_users,
    reset_user_password,
    set_user_roles,
    update_user,
)
from src.domain.models.auth import User
from src.domain.permissions import Permissions

router = APIRouter(prefix="/users", tags=["users"])

UsersReadDep = Annotated[User, Depends(require_permission(Permissions.USERS_READ))]
UsersWriteDep = Annotated[User, Depends(require_permission(Permissions.USERS_WRITE))]


def _http_error(exc: RbacError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("", response_model=list[UserRead])
def get_users(
    session: SessionDep,
    _: UsersReadDep,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    keyword: str | None = Query(default=None),
):
    return list_users(session, limit=limit, offset=offset, keyword=keyword)


@router.post("", response_model=UserRead, status_code=201)
def post_user(payload: UserCreate, session: SessionDep, _: UsersWriteDep):
    try:
        return create_user(session, payload)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="username already exists") from exc


@router.put("/{user_id}", response_model=UserRead)
def put_user(
    user_id: int,
    payload: UserUpdate,
    session: SessionDep,
    actor: UsersWriteDep,
):
    try:
        return update_user(session, actor_id=actor.id or 0, user_id=user_id, payload=payload)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.put("/{user_id}/roles", response_model=UserRead)
def put_user_roles(
    user_id: int,
    payload: UserRolesUpdate,
    session: SessionDep,
    _: UsersWriteDep,
):
    try:
        return set_user_roles(session, user_id=user_id, role_ids=payload.role_ids)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post("/{user_id}/reset-password", status_code=204, response_class=Response)
def reset_password(
    user_id: int,
    payload: PasswordReset,
    session: SessionDep,
    _: UsersWriteDep,
):
    try:
        reset_user_password(session, user_id, payload.password)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return Response(status_code=204)


@router.delete("/{user_id}", status_code=204, response_class=Response)
def remove_user(user_id: int, session: SessionDep, actor: UsersWriteDep):
    try:
        delete_user(session, actor_id=actor.id or 0, user_id=user_id)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return Response(status_code=204)
