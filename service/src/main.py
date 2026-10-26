from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import Session

from src.api.v1.router import api_router
from src.application.services.rbac_seed import ensure_rbac_seed
from src.config.settings import settings
from src.infrastructure import database
from src.infrastructure.migrations import run_migrations


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 迁移接线：默认开；测试用 autouse fixture 关掉，避免碰开发库。
    if settings.auto_create_tables:
        run_migrations(settings.database_url)
        # 迁移后补齐内置菜单/角色。幂等；管理端的人工修改不会被覆盖。
        # 与 create_db_and_tables 一致，按调用时的 database.engine 取引擎，
        # 便于测试把模块级 engine 换成内存库。
        with Session(database.engine) as session:
            ensure_rbac_seed(session)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.include_router(api_router, prefix="/api")
    return app


app = create_app()
