from fastapi import APIRouter

from src.api.v1 import routes_auth
from src.api.v1.routes_analytics import router as analytics_router
from src.api.v1.routes_data_sources import router as data_sources_router
from src.api.v1.routes_events import router as events_router
from src.api.v1.routes_jobs import router as jobs_router
from src.api.v1.routes_matches import router as matches_router
from src.api.v1.routes_menus import router as menus_router
from src.api.v1.routes_operation_logs import router as operation_logs_router
from src.api.v1.routes_roles import router as roles_router
from src.api.v1.routes_rule_sets import router as rule_sets_router
from src.api.v1.routes_system import router as system_router
from src.api.v1.routes_users import router as users_router
from src.api.v1.routes_workers import router as workers_router

api_router = APIRouter()


@api_router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "logharvest"}


api_router.include_router(routes_auth.router)
api_router.include_router(data_sources_router)
api_router.include_router(rule_sets_router)
api_router.include_router(jobs_router)
api_router.include_router(events_router)
api_router.include_router(matches_router)
api_router.include_router(workers_router)
api_router.include_router(system_router)
api_router.include_router(users_router)
api_router.include_router(roles_router)
api_router.include_router(menus_router)
api_router.include_router(operation_logs_router)
api_router.include_router(analytics_router)
