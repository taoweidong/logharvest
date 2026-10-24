from fastapi import APIRouter, HTTPException, Query
from sqlmodel import Session, select

from src.api.dependencies import AdminDep, SessionDep
from src.domain.models.entities import Worker

router = APIRouter(prefix="/workers", tags=["workers"])


def _get_worker(session: Session, worker_id: str) -> Worker:
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker not found")
    return worker


@router.post("/{worker_id}/drain")
def drain_worker(worker_id: str, session: SessionDep, _: AdminDep):
    worker = _get_worker(session, worker_id)
    worker.drain_requested = True
    worker.status = "draining"
    session.add(worker)
    session.commit()
    return {"id": worker.id, "drain_requested": worker.drain_requested}


@router.post("/{worker_id}/resume")
def resume_worker(worker_id: str, session: SessionDep, _: AdminDep):
    worker = _get_worker(session, worker_id)
    if worker.status == "offline":
        raise HTTPException(status_code=409, detail="worker is offline")
    worker.drain_requested = False
    worker.status = "online"
    session.add(worker)
    session.commit()
    return {"id": worker.id, "drain_requested": worker.drain_requested}


@router.post("/drain-version")
def drain_version(
    session: SessionDep,
    _: AdminDep,
    version: str = Query(min_length=1),
):
    workers = list(session.exec(select(Worker).where(Worker.version == version)).all())
    for worker in workers:
        worker.drain_requested = True
        worker.status = "draining"
        session.add(worker)
    session.commit()
    return {"drained": len(workers)}
