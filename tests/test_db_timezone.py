"""ROADMAP #49: every database session runs in UTC, and the service refuses to start when it does not."""
import asyncio
import logging
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from src.core import database
from src.core.db_timezone import (
    DatabaseTimezoneError,
    check_session_timezone,
    utc_connect_args,
    verify_database_timezone,
)

PG = "postgresql://u:p@localhost:5432/db"


# ---- the pure check -----------------------------------------------------------


@pytest.mark.parametrize("name", ["UTC", "Etc/UTC", "utc", " Etc/UTC "])
def test_utc_names_pass(name):
    check_session_timezone(name)


@pytest.mark.parametrize("name", ["Asia/Tashkent", "Europe/Moscow", "GMT", "+05", "", None])
def test_any_other_zone_is_critical_and_refuses_to_start(name, caplog):
    with caplog.at_level(logging.CRITICAL, logger="src.core.db_timezone"):
        with pytest.raises(DatabaseTimezoneError):
            check_session_timezone(name)
    assert any(r.levelno == logging.CRITICAL and "TimeZone" in r.getMessage() for r in caplog.records)


# ---- the startup guard on an engine -------------------------------------------


def _engine(dialect, zone):
    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda statement: SimpleNamespace(scalar=lambda: zone))

    return SimpleNamespace(dialect=SimpleNamespace(name=dialect), connect=connect)


def test_guard_accepts_a_utc_postgres_session():
    verify_database_timezone(_engine("postgresql", "Etc/UTC"))


def test_guard_rejects_a_tashkent_postgres_session():
    with pytest.raises(DatabaseTimezoneError, match="Asia/Tashkent"):
        verify_database_timezone(_engine("postgresql", "Asia/Tashkent"))


def test_guard_skips_sqlite_without_touching_the_database():
    def boom():
        raise AssertionError("SQLite has no session time zone: the guard must not connect")

    verify_database_timezone(SimpleNamespace(dialect=SimpleNamespace(name="sqlite"), connect=boom))
    verify_database_timezone(create_engine("sqlite://"))


# ---- wired into startup ---------------------------------------------------------


def _production_lifespan(monkeypatch, order):
    import src.main as main

    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main, "_apply_migrations_safely", lambda: order.append("migrations"))

    async def idle():
        await asyncio.sleep(3600)

    monkeypatch.setattr(main, "_close_expired_exams_loop", idle)
    return main


def test_startup_checks_the_zone_before_the_first_migration(monkeypatch):
    order = []
    main = _production_lifespan(monkeypatch, order)
    monkeypatch.setattr(main, "verify_database_timezone", lambda engine: order.append("zone check"))

    async def start():
        async with main.lifespan(main.app):
            order.append("serving")

    asyncio.run(start())
    assert order == ["zone check", "migrations", "serving"]


def test_startup_fails_before_any_migration_when_the_session_is_not_utc(monkeypatch):
    order = []
    main = _production_lifespan(monkeypatch, order)

    def refuse(engine):
        raise DatabaseTimezoneError("session is Asia/Tashkent")

    monkeypatch.setattr(main, "verify_database_timezone", refuse)

    async def start():
        async with main.lifespan(main.app):
            order.append("serving")

    with pytest.raises(DatabaseTimezoneError):
        asyncio.run(start())
    assert order == []  # nothing was migrated or served through a shifted session


# ---- the pin ------------------------------------------------------------------


def test_postgres_connect_args_carry_the_utc_option():
    assert utc_connect_args(PG) == {"options": "-c timezone=UTC"}
    assert utc_connect_args("postgresql+psycopg2://u:p@h/db") == {"options": "-c timezone=UTC"}


def test_existing_connect_args_and_options_are_kept_and_utc_comes_last():
    merged = utc_connect_args(PG, {"connect_timeout": 5, "options": "-c lock_timeout=5000"})
    assert merged == {"connect_timeout": 5, "options": "-c lock_timeout=5000 -c timezone=UTC"}
    assert utc_connect_args(PG + "?options=-c%20timezone%3DAsia/Tashkent")["options"].endswith(" -c timezone=UTC")
    assert utc_connect_args(PG, merged) == merged  # idempotent


def test_sqlite_never_gets_postgres_options():
    assert utc_connect_args("sqlite://") == {}
    assert utc_connect_args("sqlite:///x.db", {"check_same_thread": False}) == {"check_same_thread": False}


def test_the_service_engine_keeps_its_timeouts_and_adds_the_utc_pin(monkeypatch):
    from src.core.config import Settings

    recorded = {}
    monkeypatch.setattr(database, "create_engine", lambda url, **kwargs: recorded.update(url=url, **kwargs))
    monkeypatch.setattr(
        database, "get_settings", lambda: Settings(database_url="postgresql://u:p@db:5432/exam", app_env="testing")
    )
    database._make_engine()
    assert recorded["connect_args"] == {
        "connect_timeout": 5,
        "options": (
            "-c lock_timeout=5000 -c statement_timeout=15000 -c idle_in_transaction_session_timeout=30000"
            " -c timezone=UTC"
        ),
    }


def test_the_sqlite_engine_gets_no_postgres_options(monkeypatch):
    from src.core.config import Settings

    recorded = {}
    monkeypatch.setattr(database, "create_engine", lambda url, **kwargs: recorded.update(url=url, **kwargs))
    monkeypatch.setattr(database, "get_settings", lambda: Settings(database_url="sqlite://", app_env="testing"))
    database._make_engine()
    assert recorded["connect_args"] == {"check_same_thread": False}


def test_the_advisory_lock_connection_of_the_migration_runner_is_pinned(monkeypatch):
    import src.main as main

    recorded = {}

    def fake_create_engine(url, **kwargs):
        recorded.update(url=url, **kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(main.sqlalchemy, "create_engine", fake_create_engine)
    monkeypatch.setattr(main.settings, "database_url", PG)
    with pytest.raises(RuntimeError, match="stop here"):
        main._apply_migrations_safely()
    assert recorded["connect_args"] == {"options": "-c timezone=UTC"}


# ---- on a real PostgreSQL (the throwaway one of the class-load test) ---------------


@pytest.mark.skipif(not os.getenv("EXAM_LOAD_PG_URL"), reason="set EXAM_LOAD_PG_URL (throwaway Postgres) to run")
def test_a_real_connection_is_utc_and_keeps_the_timeouts(monkeypatch):
    from sqlalchemy import text

    from src.core.config import Settings

    url = os.environ["EXAM_LOAD_PG_URL"]
    monkeypatch.setattr(database, "get_settings", lambda: Settings(database_url=url, app_env="testing"))
    engine = database._make_engine()
    try:
        verify_database_timezone(engine)
        with engine.connect() as conn:
            values = {
                name: conn.execute(text("SELECT current_setting(:n)"), {"n": name}).scalar()
                for name in ("TimeZone", "lock_timeout", "statement_timeout", "idle_in_transaction_session_timeout")
            }
        assert values == {
            "TimeZone": "UTC", "lock_timeout": "5s", "statement_timeout": "15s",
            "idle_in_transaction_session_timeout": "30s",
        }
        # a drifted environment is caught, not silently followed (libpq sends PGTZ after ``options`` and it wins)
        monkeypatch.setenv("PGTZ", "Asia/Tashkent")
        drifted = database._make_engine()
        try:
            with pytest.raises(DatabaseTimezoneError):
                verify_database_timezone(drifted)
        finally:
            drifted.dispose()
    finally:
        engine.dispose()


# ---- no container-local clock ----------------------------------------------------


def test_the_domain_generator_stamps_creation_in_explicit_utc():
    from datetime import timedelta

    from src.domain.exam_generator import ExamVariant

    stamp = ExamVariant(variant_id="v1", variant_number=1, tasks=[], total_points=0, time_minutes=1).created_at
    assert stamp.tzinfo is not None and stamp.utcoffset() == timedelta(0)
