from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from sqlmodel import Session

from src.api.dependencies import AdminDep, SessionDep
from src.api.v1.schemas import FileTaskAttemptRead, FileTaskRead, FileTaskStatus, JobCreate, JobRead
from src.application.services.jobs import (
    RetryNotPermitted,
    cancel_job,
    create_job,
    retry_failed_tasks,
)
from src.application.services.queries import list_file_tasks, list_jobs, list_task_attempts
from src.domain.models.entities import FileTask, Job
from src.domain.source_guard import SourceNotReadOnly

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
        409: {
            "description": (
                "Idempotency-Key reused with a different request body, or the source "
                "mount was detected writable"
            )
        },
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
    try:
        job, created = create_job(session, payload, admin.username, idempotency_key)
    except SourceNotReadOnly as exc:
        # 规格 §7.2：挂载可写就拒绝处理。这是资源状态冲突，不是请求格式错误。
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
    status: FileTaskStatus | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    get_job_or_404(session, job_id)
    return list_file_tasks(session, job_id, limit=limit, offset=offset, status=status)


@router.get(
    "/{job_id}/files/{task_id}/attempts",
    response_model=list[FileTaskAttemptRead],
    responses={404: {"description": "作业或该作业下的文件任务不存在"}},
)
def get_task_attempts(job_id: int, task_id: int, session: SessionDep, _: AdminDep):
    # 任务存在且属于该作业但还没有尝试历史时返回 200 []，只有归属不符/不存在才 404。
    task = session.get(FileTask, task_id)
    if task is None or task.job_id != job_id:
        raise HTTPException(status_code=404, detail="file task not found")
    return list_task_attempts(session, job_id, task_id)


@router.post("/{job_id}/cancel", response_model=JobRead)
def post_job_cancel(job_id: int, session: SessionDep, _: AdminDep):
    return cancel_job(session, get_job_or_404(session, job_id))


@router.post(
    "/{job_id}/files/retry-failed",
    responses={409: {"description": "作业已取消，重试只会让任务永久滞留"}},
)
def retry_failed(job_id: int, session: SessionDep, _: AdminDep):
    job = get_job_or_404(session, job_id)
    try:
        return retry_failed_tasks(session, job)
    except RetryNotPermitted as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
