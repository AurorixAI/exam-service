"""
Exam Service — FastAPI application entry point.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import asyncio
from contextlib import asynccontextmanager

import sqlalchemy
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from src.core.config import get_settings
from src.core.exceptions import ExamServiceError
from src.core.middleware import RequestIDMiddleware, configure_logging
from src.api.router import _check_and_auto_close_exam, router
from src.core.database import SessionLocal
from src.models.exam_models import GeneratedExam

settings = get_settings()
configure_logging(is_development=settings.is_development)
log = logging.getLogger("exam_service")
_MIGRATION_LOCK_KEY = 41_027_003


def _apply_migrations_safely() -> None:
    """Serialize Alembic across Uvicorn workers and rolling containers.

    Every Uvicorn worker executes the lifespan hook.  Without a database lock
    two workers can issue the same ``CREATE TABLE`` concurrently on startup.
    PostgreSQL advisory locks are scoped to the whole database, unlike an
    in-process mutex, so they also protect a rolling deployment.
    """
    migration_engine = sqlalchemy.create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with migration_engine.connect() as connection:
            is_postgres = connection.dialect.name == "postgresql"
            if is_postgres:
                connection.execute(sqlalchemy.text("SELECT pg_advisory_lock(:lock_key)"), {"lock_key": _MIGRATION_LOCK_KEY})
            try:
                result = subprocess.run(
                    ["alembic", "upgrade", "head"],
                    capture_output=True,
                    text=True,
                )
            finally:
                if is_postgres:
                    connection.execute(sqlalchemy.text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": _MIGRATION_LOCK_KEY})
            if result.returncode == 0:
                log.info("Alembic migrations applied:\n%s", result.stdout)
                return
            raise RuntimeError(result.stderr or "Alembic завершился с ошибкой")
    finally:
        migration_engine.dispose()


def _close_expired_exams_once() -> None:
    """Run the blocking database sweep in a short-lived, isolated session."""
    db = SessionLocal()
    try:
        active_exams = (
            db.query(GeneratedExam)
            .filter(GeneratedExam.status == "active")
            .all()
        )
        for exam in active_exams:
            _check_and_auto_close_exam(db, exam)
    except Exception:
        db.rollback()
        log.exception("Unable to sweep expired exams")
    finally:
        db.close()


async def _close_expired_exams_loop() -> None:
    """Close timed-out live exams even when no browser is polling their API."""
    while True:
        await asyncio.to_thread(_close_expired_exams_once)
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run Alembic migrations on startup."""
    # In the test suite the schema is created by the conftest fixtures against
    # an in-memory SQLite DB; the Postgres-flavored migrations can't run there
    # (and would target a throwaway subprocess DB anyway). Skip them.
    if settings.app_env == "testing":
        yield
        return
    try:
        _apply_migrations_safely()
    except RuntimeError as exc:
        log.error("Alembic migration failed:\n%s", exc)
        sys.exit(1)
    except FileNotFoundError:
        log.warning("alembic not found — skipping migrations")
    close_expired_exams_task = asyncio.create_task(_close_expired_exams_loop())
    try:
        yield
    finally:
        close_expired_exams_task.cancel()
        try:
            await close_expired_exams_task
        except asyncio.CancelledError:
            pass


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
