from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError

from src.api.dependencies import SessionDep, require_permission
from src.api.v1.helpers import client_ip
from src.application.schemas import DataSourceCreate, DataSourceRead, DataSourceTestResult
from src.application.services.audit import commit_operation, record_operation
from src.application.services.data_sources import (
    create_data_source,
    list_data_sources,
    test_data_source_path,
)
from src.domain.models.auth import User
from src.domain.models.entities import DataSource
from src.domain.permissions import Permissions

router = APIRouter(prefix="/data-sources", tags=["data-sources"])


@router.get("", response_model=list[DataSourceRead])
def get_data_sources(
    session: SessionDep,
    _: Annotated[User, Depends(require_permission(Permissions.DATASOURCES_READ))],
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return list_data_sources(session, limit=limit, offset=offset)


@router.post("", response_model=DataSourceRead, status_code=201)
def post_data_source(
    payload: DataSourceCreate,
    request: Request,
    session: SessionDep,
    actor: Annotated[User, Depends(require_permission(Permissions.DATASOURCES_WRITE))],
):
    try:
        created = create_data_source(session, payload)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="data source name already exists") from exc

    # 数据源只有创建端点（无更新/删除接口），新 id 在业务提交后才有，审计随后单独收尾。
    record_operation(
        session,
        actor=actor,
        action="data_source.create",
        resource_type="data_source",
        resource_id=created.id,
        summary=f"创建数据源 {created.name}",
        detail={
            "root_path": created.root_path,
            "read_only": created.read_only,
            "copy_to_local": created.copy_to_local,
            "enabled": created.enabled,
        },
        ip=client_ip(request),
    )
    commit_operation(session)
    return created


@router.post("/{source_id}/test", response_model=DataSourceTestResult)
def test_source(
    source_id: int,
    session: SessionDep,
    _: Annotated[User, Depends(require_permission(Permissions.DATASOURCES_READ))],
):
    source = session.get(DataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="data source not found")
    return test_data_source_path(source)
