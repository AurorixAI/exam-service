"""Exam Service — Database session factory"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError, OperationalError, TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import get_settings


def _make_engine():
    s = get_settings()
    url = s.database_url
    # SQLite (used in tests) doesn't support connection pool args
    if url.startswith("sqlite"):
        return create_engine(url, echo=s.debug, connect_args={"check_same_thread": False})
    return create_engine(
        url,
        echo=s.debug,
        pool_pre_ping=True,
        pool_size=s.db_pool_size,
        max_overflow=s.db_max_overflow,
        pool_timeout=s.db_pool_timeout,
        pool_recycle=1800,
        connect_args={
            "connect_timeout": 5,
            # Server-side safety nets, in force from the first statement of every connection (ROADMAP #95): a row-lock
            # wait, a statement and an idle open transaction can no longer last forever.
            "options": (
                f"-c lock_timeout={int(s.db_lock_timeout_ms)}"
                f" -c statement_timeout={int(s.db_statement_timeout_ms)}"
                f" -c idle_in_transaction_session_timeout={int(s.db_idle_in_transaction_timeout_ms)}"
            ),
        },
    )


engine = _make_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency — yields a scoped SQLAlchemy Session."""
    db: Session = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def db_session() -> Generator[Session, None, None]:
    """Context manager for scripts and background tasks."""
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── transient database failures ──────────────────────────────────────────────────────────────────────────
LOCK_BUSY_MESSAGE = "Попытка сейчас обрабатывается другим запросом. Подождите несколько секунд и повторите."
DB_BUSY_MESSAGE = "Сервер временно перегружен. Подождите несколько секунд и повторите."
# lock_not_available (lock_timeout), deadlock_detected, serialization_failure: nothing was written, a retry is safe
_LOCK_SQLSTATES = {"55P03", "40P01", "40001"}


def transient_db_failure(exc: BaseException) -> str | None:
    """The student-facing text if *exc* is a database condition that a retry can cure, else None.

    Lock timeout / deadlock / statement timeout / idle-in-transaction kill / lost connection / no free pooled
    connection.  The transaction was rolled back (nothing half-written), so the caller answers 503 + Retry-After
    and the browser repeats the same request; progress and submit are idempotent under that repeat.
    """
    if isinstance(exc, PoolTimeoutError):
        return DB_BUSY_MESSAGE
    if isinstance(exc, DBAPIError):
        code = getattr(exc.orig, "pgcode", None)
        if code in _LOCK_SQLSTATES:
            return LOCK_BUSY_MESSAGE
        if isinstance(exc, OperationalError) or exc.connection_invalidated or code in {"57014", "25P03"}:
            return DB_BUSY_MESSAGE
    return None
