import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from src.api.dependencies import EngineDep, require_permission_streaming
from src.application.services.jobs import TERMINAL_JOB_STATUSES
from src.domain.models.auth import User
from src.domain.models.entities import FileTask, Job
from src.domain.permissions import Permissions

router = APIRouter(prefix="/jobs", tags=["jobs"])

SSE_POLL_SECONDS = 1.0


def _snapshot_or_none(session: Session, job_id: int) -> dict[str, Any] | None:
    job = session.get(Job, job_id)
    if job is None:
        return None

    rows = session.exec(
        select(
            FileTask.status,
            func.count(FileTask.id),
            func.coalesce(func.sum(FileTask.processed_lines), 0),
            func.coalesce(func.sum(FileTask.matched_rows), 0),
        )
        .where(FileTask.job_id == job_id)
        .group_by(FileTask.status)
    ).all()
    by_status = {str(status): int(count) for status, count, _, _ in rows}
    return {
        "job_id": job_id,
        "status": job.status,
        "total": sum(by_status.values()),
        "by_status": by_status,
        "processed_lines": sum(int(processed) for _, _, processed, _ in rows),
        "matched_rows": sum(int(matched) for _, _, _, matched in rows),
    }


def _snapshot(session: Session, job_id: int) -> dict[str, Any]:
    payload = _snapshot_or_none(session, job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="job not found")
    return payload


def _format_event(payload: dict[str, Any]) -> str:
    return f"event: progress\ndata: {json.dumps(payload, default=str)}\n\n"


async def _event_stream(
    engine: Engine,
    job_id: int,
    max_events: int,
    *,
    poll_seconds: float = SSE_POLL_SECONDS,
) -> AsyncIterator[str]:
    """Yield progress snapshots until the job ends, vanishes, or the cap is hit.

    Each poll owns a short-lived session so no database connection is pinned
    for the lifetime of the SSE response.
    """
    sent = 0
    while max_events == 0 or sent < max_events:
        with Session(engine) as snapshot_session:
            payload = _snapshot_or_none(snapshot_session, job_id)
        if payload is None:
            # The job was deleted after headers were sent; ending the stream is
            # the only valid response left.
            return
        yield _format_event(payload)
        sent += 1
        if max_events and sent >= max_events:
            return
        if payload["status"] in TERMINAL_JOB_STATUSES:
            return
        await asyncio.sleep(poll_seconds)


@router.get("/{job_id}/events")
async def job_events(
    job_id: int,
    engine: EngineDep,
    _: Annotated[User, Depends(require_permission_streaming(Permissions.JOBS_READ))],
    max_events: int = Query(default=0, ge=0, le=100),
):
    # Validate before returning a stream so unknown jobs get a normal 404.
    with Session(engine) as validation_session:
        _snapshot(validation_session, job_id)
    return StreamingResponse(
        _event_stream(engine, job_id, max_events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
