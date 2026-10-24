from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query

from src.api.dependencies import AdminDep, SessionDep
from src.api.v1.schemas import MatchRead
from src.application.services.queries import list_matches

router = APIRouter(prefix="/matches", tags=["matches"])


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@router.get("", response_model=list[MatchRead])
def get_matches(
    session: SessionDep,
    _: AdminDep,
    job_id: int | None = None,
    rule_id: str | None = None,
    file_name: str | None = None,
    severity: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    created_before: datetime | None = None,
    id_before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
):
    if job_id is None and (created_from is None or created_to is None):
        raise HTTPException(
            status_code=422,
            detail="either job_id or both created_from and created_to are required",
        )
    if created_from is not None and created_to is not None:
        created_from = _as_utc(created_from)
        created_to = _as_utc(created_to)
        if created_from > created_to:
            raise HTTPException(
                status_code=422,
                detail="created_from must not be after created_to",
            )
    if (created_before is None) != (id_before is None):
        raise HTTPException(
            status_code=422,
            detail="created_before and id_before must be provided together",
        )
    return list_matches(
        session,
        job_id=job_id,
        rule_id=rule_id,
        file_name=file_name,
        severity=severity,
        created_from=created_from,
        created_to=created_to,
        created_before=_as_utc(created_before) if created_before is not None else None,
        id_before=id_before,
        limit=limit,
    )
