from fastapi import APIRouter, Query

from src.api.dependencies import AdminDep, SessionDep
from src.api.v1.schemas import SystemStatusRead, WorkerRead
from src.application.services.queries import list_workers, system_status

router = APIRouter(tags=["system"])


@router.get("/workers", response_model=list[WorkerRead])
def get_workers(
    session: SessionDep,
    _: AdminDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    return list_workers(session, limit=limit, offset=offset)


@router.get("/system/status", response_model=SystemStatusRead)
def get_system_status(session: SessionDep, _: AdminDep):
    return system_status(session)
