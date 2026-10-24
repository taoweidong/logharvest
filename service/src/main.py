from fastapi import FastAPI

from src.api.v1.router import api_router
from src.config.settings import settings


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name)
    app.include_router(api_router, prefix="/api")
    return app


app = create_app()
