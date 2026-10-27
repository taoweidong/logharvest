from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_
from sqlmodel import Session, select

from src.domain.models.entities import FileTask, FileTaskAttempt, Job, Match, Worker, utcnow

# Workers that still report in. ``offline`` rows are historical and are never
# counted as online, but they stay in the version distribution.
ONLINE_WORKER_STATUSES = ("online", "busy", "draining")
MATCHES_WINDOW_HOURS = 24


def _age_seconds_since(value: datetime | None) -> int | None:
    """Seconds elapsed since a stored timestamp, or ``None`` when absent.

    SQLite hands back naive datetimes from aggregate columns while PostgreSQL
    sessions are forced to UTC; normalise both onto aware UTC before subtracting.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - value).total_seconds()))


def list_jobs(
    session: Session, limit: int = 50, offset: int = 0, status: str | None = None
) -> list[Job]:
    # status 下推到 SQL：前端原先在内存里过滤当前页，会得到"筛了但没筛全"的假结果。
    statement = select(Job)
    if status is not None:
        statement = statement.where(Job.status == status)
    statement = statement.order_by(Job.created_at.desc(), Job.id.desc()).offset(offset).limit(limit)
    return list(session.exec(statement).all())


def list_file_tasks(
    session: Session,
    job_id: int,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> list[FileTask]:
    statement = select(FileTask).where(FileTask.job_id == job_id)
    if status is not None:
        statement = statement.where(FileTask.status == status)
    statement = statement.order_by(FileTask.id).offset(offset).limit(limit)
    return list(session.exec(statement).all())


def list_matches(
    session: Session,
    job_id: int | None = None,
    rule_id: str | None = None,
    file_name: str | None = None,
    severity: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    created_before: datetime | None = None,
    id_before: int | None = None,
    limit: int = 50,
) -> list[Match]:
    statement = select(Match)
    if job_id is not None:
        statement = statement.where(Match.job_id == job_id)
    if rule_id:
        statement = statement.where(Match.rule_id == rule_id)
    if file_name:
        statement = statement.where(Match.file_name == file_name)
    if severity:
        statement = statement.where(Match.severity == severity)
    if created_from is not None:
        statement = statement.where(Match.created_at >= created_from)
    if created_to is not None:
        statement = statement.where(Match.created_at <= created_to)
    if created_before is not None and id_before is not None:
        statement = statement.where(
            or_(
                Match.created_at < created_before,
                and_(
                    Match.created_at == created_before,
                    Match.id < id_before,
                ),
            )
        )
    statement = statement.order_by(Match.created_at.desc(), Match.id.desc()).limit(limit)
    return list(session.exec(statement).all())


def list_workers(session: Session, limit: int = 50, offset: int = 0) -> list[Worker]:
    statement = select(Worker).order_by(Worker.id).offset(offset).limit(limit)
    return list(session.exec(statement).all())


def list_task_attempts(session: Session, job_id: int, task_id: int) -> list[FileTaskAttempt]:
    """One file task's attempt history, or ``[]`` when the task does not belong
    to that job（调用方据归属给 404，避免跨作业读到别人的重试记录）。"""
    task = session.get(FileTask, task_id)
    if task is None or task.job_id != job_id:
        return []
    statement = (
        select(FileTaskAttempt)
        .where(FileTaskAttempt.file_task_id == task_id)
        .order_by(FileTaskAttempt.attempt_no)
    )
    return list(session.exec(statement).all())


def system_status(session: Session) -> dict[str, Any]:
    counts = dict(
        session.exec(select(FileTask.status, func.count()).group_by(FileTask.status)).all()
    )
    oldest = session.exec(
        select(func.min(FileTask.scheduled_at)).where(FileTask.status == "pending")
    ).one()
    pending_oldest = None
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        pending_oldest = max(0.0, (datetime.now(UTC) - oldest).total_seconds())
    online = session.exec(
        select(func.count()).select_from(Worker).where(Worker.status.in_(ONLINE_WORKER_STATUSES))
    ).one()
    return {
        "file_tasks": counts,
        "pending_oldest_seconds": pending_oldest,
        "workers_online": online,
    }


def _status_counts(session: Session, column: Any) -> dict[str, int]:
    return {
        str(status): int(count)
        for status, count in session.exec(select(column, func.count()).group_by(column)).all()
    }


def _worker_online_count(session: Session) -> int:
    return int(
        session.exec(
            select(func.count())
            .select_from(Worker)
            .where(Worker.status.in_(ONLINE_WORKER_STATUSES))
        ).one()
        or 0
    )


def system_overview(session: Session) -> dict[str, Any]:
    """Aggregated counts for the dashboard's first screen.

    与 ``system_status`` 的分工：后者只给队列健康（文件任务计数、最老 pending、
    在线数），概览再补上作业分布、24 小时命中量与 Worker 版本分布，让前端不必
    自己拉全表去数。
    """
    cutoff = utcnow() - timedelta(hours=MATCHES_WINDOW_HOURS)
    matches_24h = int(
        session.exec(
            select(func.count()).select_from(Match).where(Match.created_at >= cutoff)
        ).one()
        or 0
    )
    oldest_pending = session.exec(
        select(func.min(FileTask.scheduled_at)).where(FileTask.status == "pending")
    ).one()
    return {
        "jobs": _status_counts(session, Job.status),
        "file_tasks": _status_counts(session, FileTask.status),
        "matches_24h": matches_24h,
        "workers": {
            "online": _worker_online_count(session),
            "by_version": {
                str(version): int(count)
                for version, count in session.exec(
                    select(Worker.version, func.count()).group_by(Worker.version)
                ).all()
            },
        },
        "oldest_pending_seconds": _age_seconds_since(oldest_pending),
    }
