from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.v1.router import api_router
from src.config.settings import settings
from src.infrastructure.migrations import run_migrations


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 迁移接线：默认开；测试用 autouse fixture 关掉，避免碰开发库。
    if settings.auto_create_tables:
        run_migrations(settings.database_url)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.include_router(api_router, prefix="/api")
    return app


app = create_app()
