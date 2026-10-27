"""操作日志查询接口（只读）。

审计表按设计只追加：没有写接口，也没有删除接口，保留期清理另行处理。
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.dependencies import SessionDep, require_permission
from src.api.v1.helpers import as_utc
from src.application.schemas import OperationLogRead
from src.application.services.audit import list_operation_logs
from src.domain.models.auth import User
from src.domain.permissions import Permissions

router = APIRouter(prefix="/operation-logs", tags=["operation-logs"])

AuditReadDep = Annotated[User, Depends(require_permission(Permissions.AUDIT_READ))]


@router.get("", response_model=list[OperationLogRead])
def get_operation_logs(
    session: SessionDep,
    _: AuditReadDep,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    actor_id: int | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
):
    # 时间范围要么成对要么都不给：半个区间在服务层没有可执行的口径，
    # 悄悄忽略另一端会让"最近 7 天"变成"全都算"，比报错更危险。沿用 /api/matches 契约。
    if (created_from is None) != (created_to is None):
        raise HTTPException(
            status_code=422,
            detail="created_from and created_to must be provided together",
        )
    if created_from is not None and created_to is not None:
        created_from = as_utc(created_from)
        created_to = as_utc(created_to)
        if created_from > created_to:
            raise HTTPException(
                status_code=422,
                detail="created_from must not be after created_to",
            )
    return list_operation_logs(
        session,
        limit=limit,
        offset=offset,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        created_from=created_from,
        created_to=created_to,
    )
