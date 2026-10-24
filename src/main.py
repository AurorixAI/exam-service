"""
Exam Service — FastAPI application entry point.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import anyio.to_thread
import sqlalchemy
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from src.core.config import get_settings
from src.core.exceptions import ExamServiceError
from src.core.middleware import RequestIDMiddleware, configure_logging
from src.api.router import CodedHTTPException, _check_and_auto_close_exam, router
from src.core.database import SessionLocal, transient_db_failure
from src.core.db_timezone import utc_connect_args, verify_database_timezone
from src.models.exam_models import GeneratedExam

settings = get_settings()
configure_logging(is_development=settings.is_development)
log = logging.getLogger("exam_service")
_MIGRATION_LOCK_KEY = 41_027_003
# Readiness probes get their OWN small pool of threads: /health must answer even when every request thread of the
# worker is busy (it is what tells an operator that the service is stuck, so it cannot be stuck with it).
_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="health-probe")
_PROBE_TIMEOUT_SECONDS = 4.0


def _apply_migrations_safely() -> None:
    """Serialize Alembic across Uvicorn workers and rolling containers.

    Every Uvicorn worker executes the lifespan hook.  Without a database lock
    two workers can issue the same ``CREATE TABLE`` concurrently on startup.
    PostgreSQL advisory locks are scoped to the whole database, unlike an
    in-process mutex, so they also protect a rolling deployment.
    """
    migration_engine = sqlalchemy.create_engine(
        settings.database_url, pool_pre_ping=True, connect_args=utc_connect_args(settings.database_url)
    )
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


# A «poisoned» exam (a print exam without its package, ...) fails on EVERY sweep.  Per exam: how many times in a row,
# when it may be tried again (exponential, 30 s ... 10 min) and when it was last reported.  The first failure logs the
# full stack once; later ones at most a one-line warning every 10 minutes.
_SWEEP_BACKOFF_START_S = 30.0
_SWEEP_BACKOFF_MAX_S = 600.0
_SWEEP_REPORT_EVERY_S = 600.0
_sweep_failures: dict[str, dict] = {}


def _sweep_clock() -> float:
    return time.monotonic()


def _note_sweep_failure(exam_id: str) -> None:
    now = _sweep_clock()
    state = _sweep_failures.get(exam_id)
    if state is None:
        log.exception("Unable to auto-close exam %s; the sweep continues with the others", exam_id)
        _sweep_failures[exam_id] = {"count": 1, "retry_at": now + _SWEEP_BACKOFF_START_S, "reported_at": now}
        return
    state["count"] += 1
    state["retry_at"] = now + min(_SWEEP_BACKOFF_START_S * 2 ** (state["count"] - 1), _SWEEP_BACKOFF_MAX_S)
    if now - state["reported_at"] >= _SWEEP_REPORT_EVERY_S:
        state["reported_at"] = now
        log.warning("Exam %s still cannot be auto-closed (%d failed sweeps in a row)", exam_id, state["count"])


def _close_expired_exams_once() -> None:
    """Run the blocking database sweep in a short-lived, isolated session."""
    db = SessionLocal()
    try:
        active_exams = (
            db.query(GeneratedExam)
            .filter(GeneratedExam.status == "active")
            .all()
        )
        # The ids are read first: after a failed exam's rollback every loaded row is expired, and one «poisoned» exam
        # (e.g. a print exam without its package) must neither stop the others from closing nor undo them.
        exam_ids = [exam.id for exam in active_exams]
        for gone in set(_sweep_failures) - set(exam_ids):
            _sweep_failures.pop(gone, None)  # closed meanwhile (by hand), cancelled or deleted
        for exam_id in exam_ids:
            state = _sweep_failures.get(exam_id)
            if state is not None and _sweep_clock() < state["retry_at"]:
                continue  # backing off
            try:
                exam = db.get(GeneratedExam, exam_id)
                if exam is not None:
                    _check_and_auto_close_exam(db, exam)
                _sweep_failures.pop(exam_id, None)
            except Exception:
                db.rollback()
                _note_sweep_failure(exam_id)
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
    """Size the request thread pool, run Alembic migrations on startup."""
    # Every handler that touches the database is a plain ``def`` (or hops to a thread with run_in_threadpool), so the
    # event loop only does I/O.  How many of them may block at once per worker is this limiter.
    anyio.to_thread.current_default_thread_limiter().total_tokens = settings.worker_threads
    # In the test suite the schema is created by the conftest fixtures against
    # an in-memory SQLite DB; the Postgres-flavored migrations can't run there
    # (and would target a throwaway subprocess DB anyway). Skip them.
    if settings.app_env == "testing":
        yield
        return
    # Every database session must run in UTC (ROADMAP #49).  Checked before the first migration: on any other zone the
    # service raises here and does not start, instead of writing shifted timestamps.
    from src.core.database import engine  # looked up at start so the check sees the live engine

    verify_database_timezone(engine)
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

    @app.exception_handler(CodedHTTPException)
    async def coded_http_error_handler(req: Request, exc: CodedHTTPException):
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code}, headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(ExamServiceError)
    async def exam_error_handler(req: Request, exc: ExamServiceError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(sqlalchemy.exc.DBAPIError)
    @app.exception_handler(sqlalchemy.exc.TimeoutError)
    async def transient_db_error_handler(req: Request, exc: Exception):
        """A lock/statement timeout, a killed idle transaction or an exhausted pool is «try again», never a bare 500."""
        message = transient_db_failure(exc)
        if message is None:
            raise exc
        log.warning("transient database failure on %s %s: %s", req.method, req.url.path, type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": message}, headers={"Retry-After": "2"})

    app.include_router(router, prefix="/api/v1")

    # Prometheus metrics
    Instrumentator(excluded_handlers=["/metrics", "/health/live", "/health/ready"]).instrument(app).expose(app)

    @app.get("/health/live", tags=["System"], summary="Liveness probe")
    async def health_live():
        """Always 200 — no dependency checks, no thread, no database: answered by the event loop itself."""
        return {"status": "alive"}

    @app.get("/version", tags=["System"], summary="Deployed release metadata")
    def release_version():
        """Return non-secret metadata for the exact deployed exam release."""
        return {
            "service": "exam-service",
            "app_version": settings.app_version,
            "release_sha": os.getenv("RELEASE_SHA", "unknown"),
            "release_ref": os.getenv("RELEASE_REF", "unknown"),
            "released_at": os.getenv("RELEASED_AT", "unknown"),
        }

    def _probe_dependencies() -> tuple[bool, bool]:
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
        return db_ok, redis_ok

    @app.get("/health/ready", tags=["System"], summary="Readiness probe")
    async def health_ready():
        """Returns 200 if DB + Redis reachable, 503 otherwise (also when the check itself does not finish in time)."""
        loop = asyncio.get_running_loop()
        try:
            db_ok, redis_ok = await asyncio.wait_for(
                loop.run_in_executor(_PROBE_EXECUTOR, _probe_dependencies), _PROBE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            log.warning("Readiness check timed out after %ss", _PROBE_TIMEOUT_SECONDS)
            db_ok = redis_ok = False

        ready = db_ok and redis_ok
        payload = {
            "status": "ready" if ready else "not_ready",
            "version": settings.app_version,
            "database": "ok" if db_ok else "error",
            "redis": "ok" if redis_ok else "error",
        }
        return JSONResponse(status_code=200 if ready else 503, content=payload)

    @app.get("/health", tags=["System"], summary="Health check (legacy)")
    async def health():
        return await health_ready()

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8003, reload=True)
