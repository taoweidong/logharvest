from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, select

from src.domain.models.entities import FileTask, Job, Match, Worker


def list_jobs(session: Session, limit: int = 50, offset: int = 0) -> list[Job]:
    statement = (
        select(Job).order_by(Job.created_at.desc(), Job.id.desc()).offset(offset).limit(limit)
    )
    return list(session.exec(statement).all())


def list_file_tasks(
    session: Session,
    job_id: int,
    limit: int = 50,
    offset: int = 0,
) -> list[FileTask]:
    statement = (
        select(FileTask)
        .where(FileTask.job_id == job_id)
        .order_by(FileTask.id)
        .offset(offset)
        .limit(limit)
    )
    return list(session.exec(statement).all())


def list_matches(
    session: Session,
    job_id: int | None = None,
    rule_id: str | None = None,
    file_name: str | None = None,
    cursor: int | None = None,
    limit: int = 50,
) -> list[Match]:
    statement = select(Match)
    if job_id is not None:
        statement = statement.where(Match.job_id == job_id)
    if rule_id:
        statement = statement.where(Match.rule_id == rule_id)
    if file_name:
        statement = statement.where(Match.file_name == file_name)
    if cursor is not None:
        statement = statement.where(Match.id < cursor)
    return list(session.exec(statement.order_by(Match.id.desc()).limit(limit)).all())


def list_workers(session: Session, limit: int = 50, offset: int = 0) -> list[Worker]:
    statement = select(Worker).order_by(Worker.id).offset(offset).limit(limit)
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
        select(func.count())
        .select_from(Worker)
        .where(Worker.status.in_(["online", "busy", "draining"]))
    ).one()
    return {
        "file_tasks": counts,
        "pending_oldest_seconds": pending_oldest,
        "workers_online": online,
    }
