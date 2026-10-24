from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError

from src.api.dependencies import AdminDep, SessionDep
from src.api.v1.schemas import DataSourceCreate, DataSourceRead
from src.application.services.data_sources import (
    create_data_source,
    list_data_sources,
    test_data_source_path,
)
from src.domain.models.entities import DataSource

router = APIRouter(prefix="/data-sources", tags=["data-sources"])


@router.get("", response_model=list[DataSourceRead])
def get_data_sources(session: SessionDep, _: AdminDep):
    return list_data_sources(session)


@router.post("", response_model=DataSourceRead, status_code=201)
def post_data_source(payload: DataSourceCreate, session: SessionDep, _: AdminDep):
    try:
        return create_data_source(session, payload)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="data source name already exists") from exc


@router.post("/{source_id}/test")
def test_source(source_id: int, session: SessionDep, _: AdminDep):
    source = session.get(DataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="data source not found")
    return test_data_source_path(source)
