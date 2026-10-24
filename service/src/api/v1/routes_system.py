from fastapi import APIRouter

from src.api.dependencies import AdminDep, SessionDep
from src.api.v1.schemas import SystemStatusRead, WorkerRead
from src.application.services.queries import list_workers, system_status

router = APIRouter(tags=["system"])


@router.get("/workers", response_model=list[WorkerRead])
def get_workers(session: SessionDep, _: AdminDep):
    return list_workers(session)


@router.get("/system/status", response_model=SystemStatusRead)
def get_system_status(session: SessionDep, _: AdminDep):
    return system_status(session)
