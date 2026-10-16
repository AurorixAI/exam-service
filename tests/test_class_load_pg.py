"""ROADMAP #95 on the real deployment shape: Postgres + Redis + ``uvicorn --workers 2`` + a latent auth-service stub.

Skipped unless a throwaway Postgres and Redis are named (nothing else in the suite needs them):

    docker run -d --name exam-load-pg -e POSTGRES_USER=exam -e POSTGRES_PASSWORD=exam -e POSTGRES_DB=exam_load \
        -p 127.0.0.1:55437:5432 postgres:16-alpine
    docker run -d --name exam-load-redis -p 127.0.0.1:56391:6379 redis:7-alpine
    EXAM_LOAD_PG_URL=postgresql://exam:exam@127.0.0.1:55437/exam_load EXAM_LOAD_REDIS_URL=redis://127.0.0.1:56391/0 \
        pytest tests/test_class_load_pg.py -q

The database is wiped of its exam rows and its other connections are terminated at the start of every scenario.
Before the fix a class of 30 left this service with 0 finished students and a dead ``/health``.
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest
import sqlalchemy as sa

from src.core.database import LOCK_BUSY_MESSAGE
from tests.load import class_load as load

PG_URL = os.getenv("EXAM_LOAD_PG_URL")
REDIS_URL = os.getenv("EXAM_LOAD_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not (PG_URL and REDIS_URL), reason="set EXAM_LOAD_PG_URL and EXAM_LOAD_REDIS_URL (throwaway containers) to run"
)
API = load.API


@pytest.mark.parametrize("students", [30, 60])
def test_a_class_writes_one_exam_without_a_hang(students):
    report = asyncio.run(load.run_load(
        students, PG_URL, REDIS_URL, progress_saves=5, reload_ratio=0.5, request_timeout=15, deadline=90,
    ))
    print("\n" + report.render())
    assert report.failures() == []
    assert report.finished_students == students
    assert report.overall_p95() < 2.0
    assert all(h["max_s"] < 1.0 for h in report.health_summary().values())  # /health never waits for the class


def test_a_class_waiting_in_the_lobby_is_started_together():
    report = asyncio.run(load.run_load(60, PG_URL, REDIS_URL, mode="lobby", request_timeout=15, deadline=90))
    print("\n" + report.render())
    assert report.failures() == [] and report.finished_students == 60


def test_the_timer_running_out_under_the_students_hands_loses_no_acknowledged_answer():
    report = asyncio.run(load.run_load(
        60, PG_URL, REDIS_URL, progress_saves=8, reload_ratio=0.5, expire_after=3, request_timeout=15, deadline=90,
    ))
    print("\n" + report.render())
    assert report.failures() == []  # incl. «every attempt submitted once; every 200 save is in the database»


def test_the_server_applies_its_timeouts_to_every_connection():
    from src.core import database
    from src.core.config import Settings

    original = database.get_settings
    database.get_settings = lambda: Settings(database_url=PG_URL, app_env="testing")
    try:
        engine = database._make_engine()
    finally:
        database.get_settings = original
    with engine.connect() as conn:
        shown = {
            name: conn.execute(sa.text(f"SHOW {name}")).scalar()
            for name in ("lock_timeout", "statement_timeout", "idle_in_transaction_session_timeout")
        }
    engine.dispose()
    assert shown == {"lock_timeout": "5s", "statement_timeout": "15s", "idle_in_transaction_session_timeout": "30s"}


def test_real_postgres_timeouts_are_recognised_as_retryable():
    """A statement that runs too long and a transaction that sits idle too long, as psycopg2 really raises them."""
    from sqlalchemy.exc import OperationalError

    from src.core import database
    from src.core.config import Settings

    original = database.get_settings
    database.get_settings = lambda: Settings(
        database_url=PG_URL, app_env="testing", db_statement_timeout_ms=300, db_idle_in_transaction_timeout_ms=700
    )
    try:
        engine = database._make_engine()
    finally:
        database.get_settings = original
    with engine.connect() as conn:
        with pytest.raises(OperationalError) as slow:
            conn.execute(sa.text("SELECT pg_sleep(3)"))
        assert database.transient_db_failure(slow.value) == database.DB_BUSY_MESSAGE
    with engine.connect() as conn:
        conn.execute(sa.text("SELECT 1"))  # opens a transaction and leaves it idle
        time.sleep(1.5)
        with pytest.raises(OperationalError) as idle:
            conn.execute(sa.text("SELECT 1"))
        assert database.transient_db_failure(idle.value) == database.DB_BUSY_MESSAGE
    engine.dispose()


def test_a_lock_wait_is_a_bounded_503_that_never_stops_the_worker_and_the_retry_is_safe():
    asyncio.run(_lock_wait_scenario())


async def _lock_wait_scenario():
    async with load.running_stack(PG_URL, REDIS_URL, extra_env={"DB_LOCK_TIMEOUT_MS": "800"}) as stack:
        sessions = []
        async with httpx.AsyncClient(base_url=stack.base + API, timeout=10) as http:
            for i in range(2):
                body = {"student_name": f"Ученик {i}", "student_id": f"00000000-0000-0000-0000-00000000000{i}"}
                r = await http.post(f"/exams/by-code/{load.CODE}/start", json=body, headers={"X-Real-IP": f"10.9.0.{i + 1}"})
                assert r.status_code == 200, r.text
                sessions.append((r.json()["session_id"], {"X-Exam-Session-Token": r.json()["session_token"]}))
            (sid_a, auth_a), (sid_b, auth_b) = sessions

            engine = sa.create_engine(PG_URL)
            blocker = engine.connect()
            blocker.execute(sa.text("SELECT id FROM exam_sessions WHERE id = :s FOR UPDATE"), {"s": sid_a})  # held open

            async def health_while_blocked():
                worst = 0.0
                for _ in range(12):
                    started = time.monotonic()
                    assert (await http.get(stack.base + "/health/live")).status_code == 200
                    worst = max(worst, time.monotonic() - started)
                    await asyncio.sleep(0.1)
                return worst

            started = time.monotonic()
            blocked, other, worst_health = await asyncio.gather(
                http.post(f"/exam-sessions/{sid_a}/progress", json={"answers": {"TASK_000": "0"}}, headers=auth_a),
                http.post(f"/exam-sessions/{sid_b}/progress", json={"answers": {"TASK_000": "9"}}, headers=auth_b),
                health_while_blocked(),
            )
            assert blocked.status_code == 503 and blocked.headers["retry-after"] == "2"
            assert blocked.json()["detail"] == LOCK_BUSY_MESSAGE
            assert 0.7 < time.monotonic() - started < 4.0  # bounded by lock_timeout, not by the 5 s test patience
            assert other.status_code == 200  # another student's save is not queued behind this attempt
            assert worst_health < 0.5  # and the event loop of the workers was never stopped by the wait

            # a finalizer holding the exam row for a while (it grades the whole class) does not stop saves either
            blocker.rollback()
            blocker.execute(sa.text("SELECT id FROM generated_exams WHERE id = :e FOR UPDATE"), {"e": load.EXAM_ID})
            saved = await http.post(f"/exam-sessions/{sid_a}/progress", json={"answers": {"TASK_000": "0"}}, headers=auth_a)
            assert saved.status_code == 200  # progress locks the attempt, not the exam
            by_code = await http.get(f"/exams/by-code/{load.CODE}")
            assert by_code.status_code == 200  # a running timer needs no exclusive lock to be read
            blocker.rollback()

            # the retry of the save that timed out: once, twice - the same single answer
            first = await http.post(f"/exam-sessions/{sid_a}/progress", json={"answers": {"TASK_000": "0"}}, headers=auth_a)
            second = await http.post(f"/exam-sessions/{sid_a}/progress", json={"answers": {"TASK_000": "0"}}, headers=auth_a)
            assert first.json() == second.json() == {"ok": True, "answered_count": 1}

            # a submit that races its own retry is graded once
            body = {"answers": {"TASK_000": "0", "TASK_009": "3"}, "time_spent_seconds": 30}
            one, two = await asyncio.gather(
                http.post(f"/exam-sessions/{sid_a}/submit", json=body, headers=auth_a),
                http.post(f"/exam-sessions/{sid_a}/submit", json=body, headers=auth_a),
            )
            assert sorted([one.status_code, two.status_code]) == [200, 400]
        blocker.close()
        engine.dispose()
