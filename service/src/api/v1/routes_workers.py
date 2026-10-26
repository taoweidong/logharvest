from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from src.api.dependencies import SessionDep, require_permission
from src.api.v1.schemas import WorkerRead
from src.application.services.queries import list_workers
from src.domain.models.auth import User
from src.domain.models.entities import Worker
from src.domain.permissions import Permissions

router = APIRouter(prefix="/workers", tags=["workers"])


def _get_worker(session: Session, worker_id: str) -> Worker:
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker not found")
    return worker


@router.get("", response_model=list[WorkerRead])
def get_workers(
    session: SessionDep,
    _: Annotated[User, Depends(require_permission(Permissions.WORKERS_READ))],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Worker 列表。此前挂在 `system` 路由下，路径与资源不同名；路由路径不变。"""
    return list_workers(session, limit=limit, offset=offset)


@router.post("/{worker_id}/drain")
def drain_worker(
    worker_id: str,
    session: SessionDep,
    _: Annotated[User, Depends(require_permission(Permissions.WORKERS_OPERATE))],
):
    worker = _get_worker(session, worker_id)
    worker.drain_requested = True
    # An offline worker must stay offline so the management view keeps showing
    # reality; the drain flag still applies when it comes back.
    if worker.status != "offline":
        worker.status = "draining"
    session.add(worker)
    session.commit()
    return {
        "id": worker.id,
        "drain_requested": worker.drain_requested,
        "status": worker.status,
    }


@router.post("/{worker_id}/resume")
def resume_worker(
    worker_id: str,
    session: SessionDep,
    _: Annotated[User, Depends(require_permission(Permissions.WORKERS_OPERATE))],
):
    worker = _get_worker(session, worker_id)
    if worker.status == "offline":
        raise HTTPException(status_code=409, detail="worker is offline")
    worker.drain_requested = False
    worker.status = "online"
    session.add(worker)
    session.commit()
    return {
        "id": worker.id,
        "drain_requested": worker.drain_requested,
        "status": worker.status,
    }


@router.post("/drain-version")
def drain_version(
    session: SessionDep,
    _: Annotated[User, Depends(require_permission(Permissions.WORKERS_OPERATE))],
    version: str = Query(min_length=1),
):
    # A rolling upgrade must also take effect after a restart, so offline
    # workers get the flag too; only live rows are reported as draining,
    # because flipping offline rows to ``draining`` would misreport reality.
    workers = list(session.exec(select(Worker).where(Worker.version == version)).all())
    drained = 0
    for worker in workers:
        worker.drain_requested = True
        if worker.status != "offline":
            worker.status = "draining"
            drained += 1
        session.add(worker)
    session.commit()
    return {"drained": drained, "flagged": len(workers) - drained}
