from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError

from src.api.dependencies import SessionDep, require_permission
from src.api.v1.schemas import MenuCreate, MenuRead, MenuUpdate
from src.application.services.rbac import (
    RbacError,
    create_menu,
    delete_menu,
    list_menus,
    menu_tree,
    update_menu,
)
from src.domain.models.auth import User
from src.domain.permissions import Permissions

router = APIRouter(prefix="/menus", tags=["menus"])

MenusReadDep = Annotated[User, Depends(require_permission(Permissions.MENUS_READ))]
MenusWriteDep = Annotated[User, Depends(require_permission(Permissions.MENUS_WRITE))]


def _http_error(exc: RbacError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


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
def post_menu(payload: MenuCreate, session: SessionDep, _: MenusWriteDep):
    try:
        return create_menu(session, payload)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="menu name already exists") from exc


@router.put("/{menu_id}", response_model=MenuRead)
def put_menu(menu_id: int, payload: MenuUpdate, session: SessionDep, _: MenusWriteDep):
    try:
        return update_menu(session, menu_id, payload)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.delete("/{menu_id}", status_code=204, response_class=Response)
def remove_menu(menu_id: int, session: SessionDep, _: MenusWriteDep):
    try:
        delete_menu(session, menu_id)
    except RbacError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    return Response(status_code=204)
