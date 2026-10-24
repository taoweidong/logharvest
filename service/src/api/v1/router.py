from fastapi import APIRouter

from src.api.v1.routes_data_sources import router as data_sources_router
from src.api.v1.routes_events import router as events_router
from src.api.v1.routes_jobs import router as jobs_router
from src.api.v1.routes_matches import router as matches_router
from src.api.v1.routes_rule_sets import router as rule_sets_router
from src.api.v1.routes_system import router as system_router
from src.api.v1.routes_workers import router as workers_router

api_router = APIRouter()
api_router.include_router(data_sources_router)
api_router.include_router(rule_sets_router)
api_router.include_router(jobs_router)
api_router.include_router(events_router)
api_router.include_router(matches_router)
api_router.include_router(workers_router)
api_router.include_router(system_router)


@api_router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "logharvest"}
