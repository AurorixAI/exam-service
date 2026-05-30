"""Exam Service — Database session factory"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
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
        pool_size=5,
        max_overflow=10,
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
