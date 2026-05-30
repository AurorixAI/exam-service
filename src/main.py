"""
Exam Service — FastAPI application entry point.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from contextlib import asynccontextmanager

import sqlalchemy
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from src.core.config import get_settings
from src.core.exceptions import ExamServiceError
from src.core.middleware import RequestIDMiddleware, configure_logging
from src.api.router import router

settings = get_settings()
configure_logging(is_development=settings.is_development)
log = logging.getLogger("exam_service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run Alembic migrations on startup."""
    try:
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log.info("Alembic migrations applied:\n%s", result.stdout)
        else:
            log.error("Alembic migration failed:\n%s", result.stderr)
            sys.exit(1)
    except FileNotFoundError:
        log.warning("alembic not found — skipping migrations")
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
    )

    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=r"http://localhost:\d+",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ExamServiceError)
    async def exam_error_handler(req: Request, exc: ExamServiceError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    app.include_router(router, prefix="/api/v1")

    # Prometheus metrics
    Instrumentator(excluded_handlers=["/metrics", "/health/live", "/health/ready"]).instrument(app).expose(app)

    @app.get("/health/live", tags=["System"], summary="Liveness probe")
    def health_live():
        """Always 200 — no dependency checks."""
        return {"status": "alive"}

    @app.get("/health/ready", tags=["System"], summary="Readiness probe")
    def health_ready():
        """Returns 200 if DB + Redis reachable, 503 otherwise."""
        from src.core.database import engine
        from src.core.redis_client import get_redis_client

        db_ok = True
        redis_ok = True

        try:
            with engine.connect() as conn:
                conn.execute(sqlalchemy.text("SELECT 1"))
        except Exception as exc:
            log.warning("Readiness DB check failed: %s", exc)
            db_ok = False

        try:
            get_redis_client().ping()
        except Exception as exc:
            log.warning("Readiness Redis check failed: %s", exc)
            redis_ok = False

        ready = db_ok and redis_ok
        payload = {
            "status": "ready" if ready else "not_ready",
            "version": settings.app_version,
            "database": "ok" if db_ok else "error",
            "redis": "ok" if redis_ok else "error",
        }
        return JSONResponse(status_code=200 if ready else 503, content=payload)

    @app.get("/health", tags=["System"], summary="Health check (legacy)")
    def health():
        return health_ready()

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8003, reload=True)
