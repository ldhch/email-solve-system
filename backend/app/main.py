"""FastAPI entrypoint (M-01)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.auth import router as auth_router
from app.api.conversations import router as conversations_router
from app.api.inbox import router as inbox_router
from app.api.system import router as system_router
from app.api.tickets import router as tickets_router
from app.config import get_settings
from app.core.logging import setup_logging
from app.db.session import init_db

setup_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create tables + seed on startup (create_all, no migrations)."""

    init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="After-sales email auto-reply backend (Phase 2)",
        lifespan=lifespan,
    )
    for router in (
        system_router,
        auth_router,
        inbox_router,
        conversations_router,
        tickets_router,
    ):
        app.include_router(router)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Async handler (TECH M-21): default handlers run in a thread pool."""

        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "VALIDATION_ERROR"})

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {"app": settings.app_name, "phase": 2, "status": "ok"}

    return app


app = create_app()
