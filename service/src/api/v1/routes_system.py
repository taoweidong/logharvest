from fastapi import APIRouter, HTTPException, status

from src.api.dependencies import AdminDep, EngineDep, SessionDep
from src.api.v1.schemas import (
    MaintenanceRunRead,
    SystemOverviewRead,
    SystemStatusRead,
)
from src.application.services.maintenance import MaintenanceRequiresPostgres, run_maintenance
from src.application.services.queries import system_overview, system_status

router = APIRouter(tags=["system"])


@router.get("/system/status", response_model=SystemStatusRead)
def get_system_status(session: SessionDep, _: AdminDep):
    return system_status(session)


@router.get("/system/overview", response_model=SystemOverviewRead)
def get_system_overview(session: SessionDep, _: AdminDep):
    return system_overview(session)


@router.post(
    "/system/maintenance",
    response_model=MaintenanceRunRead,
    responses={503: {"description": "队列数据库不是 PostgreSQL"}},
)
def trigger_maintenance(engine: EngineDep, _: AdminDep):
    """Run the server-side compensation sweeps once, right now.

    Worker 循环里的同类清扫只在有存活进程时执行；这里给运维（以及外部 cron /
    compose job）一个无需重启 Worker 就能触发回收、降级与作业收尾的入口。可重复
    执行，没有待处理项时三个计数都是 0。

    僵死回收是 PostgreSQL 专有 SQL，所以在 SQLite 等非 PostgreSQL 连接上返回 503
    并说明原因，而不是抛一个看不懂的 500。
    """
    try:
        return run_maintenance(engine)
    except MaintenanceRequiresPostgres as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
