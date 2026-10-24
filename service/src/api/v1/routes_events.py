import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.engine import Connection, Engine
from sqlmodel import Session, select

from src.api.dependencies import AdminDep, SessionDep
from src.domain.models.entities import FileTask, Job

router = APIRouter(prefix="/jobs", tags=["jobs"])

SSE_POLL_SECONDS = 1.0


def _snapshot(session: Session, job_id: int) -> dict[str, Any]:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

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


def _format_event(payload: dict[str, Any]) -> str:
    return f"event: progress\ndata: {json.dumps(payload, default=str)}\n\n"


async def _event_stream(
    bind: Engine | Connection,
    job_id: int,
    max_events: int,
    *,
    poll_seconds: float = SSE_POLL_SECONDS,
) -> AsyncIterator[str]:
    sent = 0
    while max_events == 0 or sent < max_events:
        with Session(bind) as snapshot_session:
            payload = _snapshot(snapshot_session, job_id)
        yield _format_event(payload)
        sent += 1
        if max_events and sent >= max_events:
            break
        await asyncio.sleep(poll_seconds)


@router.get("/{job_id}/events")
async def job_events(
    job_id: int,
    session: SessionDep,
    _: AdminDep,
    max_events: int = Query(default=0, ge=0, le=100),
):
    # Validate before returning a stream so unknown jobs get a normal 404.
    _snapshot(session, job_id)
    return StreamingResponse(
        _event_stream(session.get_bind(), job_id, max_events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
