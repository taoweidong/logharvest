from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from src.api.v1.schemas import JobCreate, JobFileFilter
from src.domain.archive.models import ScanLimitError
from src.domain.archive.reader import (
    ARCHIVE_SUFFIXES,
    discover_archives,
    normalize_suffixes,
)
from src.domain.models.entities import DataSource, FileTask, Job, RuleSet

TERMINAL_JOB_STATUSES = frozenset({"succeeded", "partial", "failed", "cancelled"})
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
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used with a different request body",
        )


def _effective_suffixes(filters: JobFileFilter) -> set[str] | None:
    if filters.suffixes is None:
        return None
    effective = normalize_suffixes(filters.suffixes) & ARCHIVE_SUFFIXES
    if not effective:
        supported = ", ".join(sorted(ARCHIVE_SUFFIXES))
        raise HTTPException(
            status_code=400,
            detail=(
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
        raise HTTPException(status_code=400, detail="Idempotency-Key must not be blank")
    if normalized_key:
        existing = _find_job_by_idempotency_key(session, created_by, normalized_key)
        if existing is not None:
            _ensure_idempotent_match(existing, payload)
            return existing, False

    source = session.get(DataSource, payload.data_source_id)
    if source is None or not source.enabled:
        raise HTTPException(status_code=404, detail="data source not found or disabled")
    rule_set = session.get(RuleSet, payload.rule_set_id)
    if rule_set is None:
        raise HTTPException(status_code=404, detail="rule set not found")

    root = Path(source.root_path).resolve()
    source_path = Path(payload.source_path).resolve()
    if not source_path.is_relative_to(root):
        raise HTTPException(status_code=400, detail="source_path is outside data source root")
    if not source_path.is_dir():
        raise HTTPException(status_code=400, detail="source_path is not a directory")

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
            raise HTTPException(
                status_code=503,
                detail="source_path scan timed out; narrow the directory or filters",
            ) from exc
        raise HTTPException(
            status_code=400,
            detail="too many files in source directory",
        ) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"cannot scan source_path: {exc}") from exc

    now = datetime.now(UTC)
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

    now = datetime.now(UTC)
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
