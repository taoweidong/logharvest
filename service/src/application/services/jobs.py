from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from src.application.schemas import JobCreate, JobFileFilter
from src.config.settings import settings
from src.domain.archive.models import ScanLimitError
from src.domain.archive.reader import (
    ARCHIVE_SUFFIXES,
    discover_archives,
    normalize_suffixes,
)
from src.domain.models.entities import DataSource, FileTask, Job, RuleSet, utcnow
from src.domain.source_guard import guard_source

TERMINAL_JOB_STATUSES = frozenset({"succeeded", "partial", "failed", "cancelled"})


class RetryNotPermitted(RuntimeError):
    """作业已取消：重新入队的任务永远不会再被领取，只会永久滞留。"""


class JobCreateError(RuntimeError):
    """作业创建/幂等校验的业务错误；``status_code`` 由路由映射到 HTTP。

    与 ``RbacError`` 同一范式：服务层不 import FastAPI，错误语义（状态码 +
    detail 文案）在这里声明，``routes_jobs`` 统一翻译成 ``HTTPException``。
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


SCAN_DEADLINE_SECONDS = 10.0


def _utc_from_timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


def _find_job_by_idempotency_key(
    session: Session,
    created_by: str,
    idempotency_key: str,
) -> Job | None:
    return session.exec(
        select(Job).where(
            Job.created_by == created_by,
            Job.idempotency_key == idempotency_key,
        )
    ).first()


def _ensure_idempotent_match(existing: Job, payload: JobCreate) -> None:
    existing_filter = JobFileFilter.model_validate(existing.file_filter)
    request_body_matches = (
        existing.name == payload.name
        and existing.data_source_id == payload.data_source_id
        and existing.source_path == str(Path(payload.source_path).resolve())
        and existing.rule_set_id == payload.rule_set_id
        and existing_filter == payload.file_filter
    )
    if not request_body_matches:
        raise JobCreateError(
            409,
            "Idempotency-Key was already used with a different request body",
        )


def _effective_suffixes(filters: JobFileFilter) -> set[str] | None:
    if filters.suffixes is None:
        return None
    effective = normalize_suffixes(filters.suffixes) & ARCHIVE_SUFFIXES
    if not effective:
        supported = ", ".join(sorted(ARCHIVE_SUFFIXES))
        raise JobCreateError(
            400,
            (
                "file_filter.suffixes must contain at least one compressed archive "
                f"suffix ({supported})"
            ),
        )
    return effective


def create_job(
    session: Session,
    payload: JobCreate,
    created_by: str,
    idempotency_key: str | None,
) -> tuple[Job, bool]:
    normalized_key = idempotency_key.strip() if idempotency_key is not None else None
    if normalized_key == "":
        raise JobCreateError(400, "Idempotency-Key must not be blank")
    if normalized_key:
        existing = _find_job_by_idempotency_key(session, created_by, normalized_key)
        if existing is not None:
            _ensure_idempotent_match(existing, payload)
            return existing, False

    source = session.get(DataSource, payload.data_source_id)
    if source is None or not source.enabled:
        raise JobCreateError(404, "data source not found or disabled")
    rule_set = session.get(RuleSet, payload.rule_set_id)
    if rule_set is None:
        raise JobCreateError(404, "rule set not found")

    root = Path(source.root_path).resolve()
    source_path = Path(payload.source_path).resolve()
    if not source_path.is_relative_to(root):
        raise JobCreateError(400, "source_path is outside data source root")
    if not source_path.is_dir():
        raise JobCreateError(400, "source_path is not a directory")

    # 只读校验必须在扫描之前完成：一旦挂载被判定可写，后面的规划与执行都不该发生
    # （规格 §7.2）。按真正要扫描的目录判定，嵌套挂载可以各自不同。
    guard_source(source_path, policy=settings.nas_read_only_policy)

    source_id = source.id
    rule_set_id = rule_set.id
    filters = payload.file_filter
    suffixes = _effective_suffixes(filters)

    # Release the read transaction before the potentially slow NAS scan so the
    # connection is not held while walking a large directory tree.
    session.rollback()

    try:
        files = discover_archives(
            source_path,
            recursive=filters.recursive,
            max_depth=filters.max_depth,
            suffixes=suffixes,
            max_files=filters.max_files,
            deadline_seconds=SCAN_DEADLINE_SECONDS,
        )
        file_stats = [(path, path.stat()) for path in files]
    except ScanLimitError as exc:
        if exc.reason == "deadline":
            raise JobCreateError(
                503,
                "source_path scan timed out; narrow the directory or filters",
            ) from exc
        raise JobCreateError(400, "too many files in source directory") from exc
    except OSError as exc:
        raise JobCreateError(400, f"cannot scan source_path: {exc}") from exc

    now = utcnow()
    job = Job(
        name=payload.name,
        data_source_id=source_id,
        source_path=str(source_path),
        file_filter=filters.model_dump(mode="json"),
        rule_set_id=rule_set_id,
        created_by=created_by,
        idempotency_key=normalized_key,
        total_files=len(files),
        total_bytes=sum(stat.st_size for _, stat in file_stats),
        status="running" if files else "succeeded",
        started_at=now,
        finished_at=None if files else now,
    )
    session.add(job)
    try:
        session.flush()
        for path, stat in file_stats:
            session.add(
                FileTask(
                    job_id=job.id,
                    source_path=str(path),
                    file_name=path.name,
                    size_bytes=stat.st_size,
                    mtime=_utc_from_timestamp(stat.st_mtime),
                )
            )
        session.commit()
    except IntegrityError:
        # A concurrent request may have created the same idempotency key first.
        session.rollback()
        if normalized_key:
            existing = _find_job_by_idempotency_key(session, created_by, normalized_key)
            if existing is not None:
                _ensure_idempotent_match(existing, payload)
                return existing, False
        raise

    session.refresh(job)
    return job, True


def cancel_job(session: Session, job: Job) -> Job:
    if job.status in TERMINAL_JOB_STATUSES:
        return job

    now = utcnow()
    job.cancel_requested = True
    session.execute(
        update(FileTask)
        .where(FileTask.job_id == job.id, FileTask.status == "pending")
        .values(status="cancelled", finished_at=now)
    )
    running_task = session.exec(
        select(FileTask.id).where(
            FileTask.job_id == job.id,
            FileTask.status == "running",
        )
    ).first()
    if running_task is None:
        job.status = "cancelled"
        job.finished_at = now
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def retry_failed_tasks(session: Session, job: Job, *, limit: int = 200) -> dict[str, int]:
    """Return terminal failed tasks of a job to the queue, keeping attempt history.

    attempts 不重置：手动重试是一次新尝试，仍受 max_attempts 约束，避免死循环刷库。
    重新入队后作业必须回到 ``running`` 并清掉 ``finished_at``，否则终态汇总与取消
    逻辑都看不到它；``remaining_failed`` 让 ``limit`` 的截断对操作者可见。

    已取消的作业直接拒绝：``CLAIM_SQL`` 永远不会再领取它的任务，把它们置回
    ``pending`` 只会让作业与队列永久滞留，并让概览的积压指标一直报警。
    """
    if job.cancel_requested or job.status == "cancelled":
        raise RetryNotPermitted("cancelled jobs cannot be retried")

    failed_total = int(
        session.exec(
            select(func.count(FileTask.id)).where(
                FileTask.job_id == job.id, FileTask.status == "failed"
            )
        ).one()
        or 0
    )
    tasks = list(
        session.exec(
            select(FileTask)
            .where(FileTask.job_id == job.id, FileTask.status == "failed")
            .order_by(FileTask.id)
            .limit(limit)
        ).all()
    )
    for task in tasks:
        task.status = "pending"
        task.error = None
        task.finished_at = None
        task.worker_id = None
        task.scheduled_at = utcnow()
        session.add(task)
    if tasks and job.status in TERMINAL_JOB_STATUSES:
        job.status = "running"
        job.finished_at = None
        session.add(job)
    session.commit()
    return {"retried": len(tasks), "remaining_failed": max(0, failed_total - len(tasks))}
