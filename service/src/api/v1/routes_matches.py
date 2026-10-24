from fastapi import APIRouter, Query

from src.api.dependencies import AdminDep, SessionDep
from src.api.v1.schemas import MatchRead
from src.application.services.queries import list_matches

router = APIRouter(prefix="/matches", tags=["matches"])


@router.get("", response_model=list[MatchRead])
def get_matches(
    session: SessionDep,
    _: AdminDep,
    job_id: int | None = None,
    rule_id: str | None = None,
    file_name: str | None = None,
    cursor: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
):
    return list_matches(
        session,
        job_id=job_id,
        rule_id=rule_id,
        file_name=file_name,
        cursor=cursor,
        limit=limit,
    )
