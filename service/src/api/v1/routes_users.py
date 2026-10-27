from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from src.api.dependencies import SessionDep, require_permission
from src.api.v1.helpers import client_ip
from src.application.schemas import (
    PasswordReset,
    UserCreate,
    UserRead,
    UserRolesUpdate,
    UserUpdate,
)
from src.application.services.audit import commit_operation, detach_actor, record_operation
from src.application.services.rbac import (
    RbacError,
    create_user,
    delete_user,
    list_users,
    reset_user_password,
    set_user_roles,
    update_user,
)
from src.domain.models.auth import User, UserRole
from src.domain.permissions import Permissions

router = APIRouter(prefix="/users", tags=["users"])

UsersReadDep = Annotated[User, Depends(require_permission(Permissions.USERS_READ))]
UsersWriteDep = Annotated[User, Depends(require_permission(Permissions.USERS_WRITE))]


def _http_error(exc: RbacError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _user_snapshot(session: SessionDep, user_id: int) -> dict[str, object]:
    """审计用的变更前面貌；账号不存在时返回空字典，服务层随即给出 404。"""
    user = session.get(User, user_id)
    if user is None:
        return {}
    return {"username": user.username, "nickname": user.nickname, "is_active": user.is_active}


def _role_ids(session: SessionDep, user_id: int) -> list[int]:
    rows = session.exec(select(UserRole.role_id).where(UserRole.user_id == user_id)).all()
    return sorted(int(role_id) for role_id in rows)


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
def post_user(
    payload: UserCreate,
    request: Request,
    session: SessionDep,
    actor: UsersWriteDep,
):
    try:
        created = create_user(session, payload)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="username already exists") from exc

    # 新 id 只在业务提交之后存在，这一条只能落在业务事务之外（口令不进 detail）。
    record_operation(
        session,
        actor=actor,
        action="user.create",
        resource_type="user",
        resource_id=created.id,
        summary=f"创建用户 {created.username}",
        detail={
            "after": {
                "username": created.username,
                "nickname": created.nickname,
                "is_active": created.is_active,
                "role_ids": [role.id for role in created.roles],
            }
        },
        ip=client_ip(request),
    )
    commit_operation(session)
    return created


@router.put("/{user_id}", response_model=UserRead)
def put_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    session: SessionDep,
    actor: UsersWriteDep,
):
    before = _user_snapshot(session, user_id)
    record_operation(
        session,
        actor=actor,
        action="user.update",
        resource_type="user",
        resource_id=user_id,
        summary=f"更新用户 {before.get('username') or user_id}",
        detail={"before": before, "after": payload.model_dump(exclude_unset=True)},
        ip=client_ip(request),
    )
    try:
        return update_user(session, actor_id=actor.id or 0, user_id=user_id, payload=payload)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.put("/{user_id}/roles", response_model=UserRead)
def put_user_roles(
    user_id: int,
    payload: UserRolesUpdate,
    request: Request,
    session: SessionDep,
    actor: UsersWriteDep,
):
    before = _role_ids(session, user_id)
    record_operation(
        session,
        actor=actor,
        action="user.assign_roles",
        resource_type="user",
        resource_id=user_id,
        summary=f"调整用户 {user_id} 的角色",
        detail={"before_role_ids": before, "after_role_ids": sorted(set(payload.role_ids))},
        ip=client_ip(request),
    )
    try:
        return set_user_roles(session, user_id=user_id, role_ids=payload.role_ids)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post("/{user_id}/reset-password", status_code=204, response_class=Response)
def reset_password(
    user_id: int,
    payload: PasswordReset,
    request: Request,
    session: SessionDep,
    actor: UsersWriteDep,
):
    record_operation(
        session,
        actor=actor,
        action="user.reset_password",
        resource_type="user",
        resource_id=user_id,
        summary="重置口令",
        detail={},
        ip=client_ip(request),
    )
    try:
        reset_user_password(session, user_id, payload.password)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return Response(status_code=204)


@router.delete("/{user_id}", status_code=204, response_class=Response)
def remove_user(
    user_id: int,
    request: Request,
    session: SessionDep,
    actor: UsersWriteDep,
):
    before = _user_snapshot(session, user_id)
    record_operation(
        session,
        actor=actor,
        action="user.delete",
        resource_type="user",
        resource_id=user_id,
        summary=f"删除用户 {before.get('username') or user_id}",
        detail={"before": before},
        ip=client_ip(request),
    )
    # 置空必须排在删除之前：与删除共用同一事务，且 SQLite 不执行外键的 SET NULL。
    detach_actor(session, actor_id=user_id)
    try:
        delete_user(session, actor_id=actor.id or 0, user_id=user_id)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return Response(status_code=204)
