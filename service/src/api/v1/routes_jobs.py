from fastapi import APIRouter, Header, HTTPException, Query, Response
from sqlmodel import Session

from src.api.dependencies import AdminDep, SessionDep
from src.api.v1.schemas import FileTaskRead, JobCreate, JobRead
from src.application.services.jobs import (
    TERMINAL_JOB_STATUSES,
    cancel_job,
    create_job,
    retry_failed_tasks,
)
from src.application.services.queries import list_file_tasks, list_jobs
from src.domain.models.entities import Job

router = APIRouter(prefix="/jobs", tags=["jobs"])


def get_job_or_404(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("", response_model=list[JobRead])
def get_jobs(
    session: SessionDep,
    _: AdminDep,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return list_jobs(session, limit=limit, offset=offset)


@router.post(
    "",
    response_model=JobRead,
    status_code=201,
    responses={
        200: {
            "model": JobRead,
            "description": "Existing job replayed for the same Idempotency-Key",
        },
        400: {"description": "Blank Idempotency-Key, invalid path, or unsupported suffixes"},
        409: {"description": "Idempotency-Key reused with a different request body"},
        503: {"description": "Source directory scan exceeded the 10 second deadline"},
    },
)
def post_job(
    payload: JobCreate,
    response: Response,
    session: SessionDep,
    admin: AdminDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    job, created = create_job(session, payload, admin.username, idempotency_key)
    if not created:
        response.status_code = 200
    return job


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: int, session: SessionDep, _: AdminDep):
    return get_job_or_404(session, job_id)


@router.get("/{job_id}/files", response_model=list[FileTaskRead])
def get_job_files(
    job_id: int,
    session: SessionDep,
    _: AdminDep,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    get_job_or_404(session, job_id)
    return list_file_tasks(session, job_id, limit=limit, offset=offset)


@router.post("/{job_id}/cancel", response_model=JobRead)
def post_job_cancel(job_id: int, session: SessionDep, _: AdminDep):
    return cancel_job(session, get_job_or_404(session, job_id))


@router.post("/{job_id}/files/retry-failed")
def retry_failed(job_id: int, session: SessionDep, _: AdminDep):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in TERMINAL_JOB_STATUSES and job.status != "cancelled":
        raise HTTPException(status_code=409, detail="job is not retryable")
    return {"retried": retry_failed_tasks(session, job_id)}
