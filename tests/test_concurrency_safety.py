"""ROADMAP #95: a class writing one exam must not hang the service.

The defect: ``progress`` / ``submit`` / ``start`` were ``async def`` handlers on a BLOCKING SQLAlchemy session.  A
request that waited for the shared ``generated_exams`` row lock blocked the whole event loop of its worker, while
another request - suspended at ``await`` (auth-service) INSIDE its open transaction - held that very lock and could
not resume.  Two workers, two such pairs: the service stopped answering, ``/health`` included.

These tests pin the rules that make that impossible, on the SQLite suite; the real thing (Postgres, two uvicorn
workers, 30-60 concurrent students) is ``tests/load`` + ``tests/test_class_load_pg.py``.

1. No handler blocks the loop: a route with nothing to await is a plain ``def`` (FastAPI runs it in a thread).
2. No ``await`` after a lock: no async function takes a row lock / finalizes and THEN awaits.
3. No transaction is open while a network call is awaited (check-student, start, by-code).
4. A save or a submit locks the student's own attempt, never the exam row the whole class shares.
5. A lock/statement timeout is a 503 + Retry-After in Russian, and the retry is safe (no double submit, no lost answer).
6. ``/health`` answers within a bound even when the database check hangs.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import time
from datetime import datetime, timedelta

import anyio.to_thread
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Query

from src.api import router as router_module
from src.core import database
from src.core.database import DB_BUSY_MESSAGE, LOCK_BUSY_MESSAGE, transient_db_failure
from tests.conftest import make_exam_in_db, make_session_in_db, make_task

API = "/api/v1"
ROUTER_TREE = ast.parse(inspect.getsource(router_module))
ASYNC_FUNCTIONS = [n for n in ast.walk(ROUTER_TREE) if isinstance(n, ast.AsyncFunctionDef)]
AWAITS = (ast.Await, ast.AsyncWith, ast.AsyncFor)


# ── 1 + 2: the shape of the code ───────────────────────────────────────────────────────────────────────────────
def test_an_async_function_has_something_to_await():
    """``async def`` + a blocking session + nothing to await is the defect itself: it must be a plain ``def``."""
    useless = [n.name for n in ASYNC_FUNCTIONS if not any(isinstance(x, AWAITS) for x in ast.walk(n))]
    assert useless == []


@pytest.mark.parametrize(
    "name", ["save_exam_progress", "submit_exam", "get_session_status", "get_results", "list_textbooks"]
)
def test_database_routes_without_network_calls_are_threadpool_functions(name):
    assert not inspect.iscoroutinefunction(getattr(router_module, name))


def test_no_async_function_takes_a_lock_and_then_awaits():
    """Static form of rule 2: lock / finalize / auto-close, and an ``await`` after it in the same function."""
    lockers = {"with_for_update", "_check_and_auto_close_exam", "_finalize_exam"}
    offenders = []
    for func in ASYNC_FUNCTIONS:
        events = []
        for node in ast.walk(func):
            if isinstance(node, AWAITS):
                events.append((node.lineno, "await"))
            elif isinstance(node, ast.Attribute) and node.attr in lockers:
                events.append((node.lineno, "lock"))
            elif isinstance(node, ast.Name) and node.id in lockers:
                events.append((node.lineno, "lock"))
        events.sort()
        kinds = [kind for _, kind in events]
        if "lock" in kinds and "await" in kinds[kinds.index("lock"):]:
            offenders.append(func.name)
    assert offenders == []


# ── 3: no transaction across a network call ───────────────────────────────────────────────────────────────────
def _open_exam(db, code, *, status="active", started_minutes_ago=5):
    exam = make_exam_in_db(db, tasks=[make_task("TASK_001", answer="3"), make_task("TASK_002", answer="5")], status=status)
    exam.access_code = code
    if status == "active":
        started = datetime.utcnow() - timedelta(minutes=started_minutes_ago)
        exam.settings = {**exam.settings, "started_at": started.isoformat()}
    db.commit()
    return exam


class _NetworkProbe:
    """Stands in for auth-service and records whether the session had a transaction open at that moment."""

    def __init__(self, db):
        self.db = db
        self.in_transaction = []

    async def canonical(self, exam, *, student_id, student_name, identifier=None):
        self.in_transaction.append(self.db.in_transaction())
        await asyncio.sleep(0)
        return {"id": student_id or "STU_X", "display_id": identifier, "full_name": student_name or "Ученик", "class_level": 8}

    async def lobby(self, exam):
        self.in_transaction.append(self.db.in_transaction())
        await asyncio.sleep(0)


@pytest.fixture
def probe(db, monkeypatch):
    p = _NetworkProbe(db)
    monkeypatch.setattr(router_module, "_canonical_student_for_exam", p.canonical)
    monkeypatch.setattr(router_module, "_require_public_lobby_available", p.lobby)
    return p


def test_start_awaits_auth_service_with_no_transaction_open(client, db, probe):
    _open_exam(db, "NOTX0001")
    resp = client.post(f"{API}/exams/by-code/NOTX0001/start", json={"student_name": "Асель", "student_id": "STU-1"})
    assert resp.status_code == 200 and resp.json()["status"] == "in_progress"
    assert probe.in_transaction == [False]


def test_check_student_awaits_auth_service_with_no_transaction_open(client, db, probe):
    _open_exam(db, "NOTX0002")
    resp = client.post(f"{API}/exams/by-code/NOTX0002/check-student", json={"student_id": "STU-2", "student_name": "Асель"})
    assert resp.status_code == 200 and resp.json()["enrolled"] is True
    assert probe.in_transaction == [False]


def test_lobby_entry_awaits_the_lesson_check_and_auth_with_no_transaction_open(client, db, probe):
    _open_exam(db, "NOTX0003", status="lobby")
    assert client.get(f"{API}/exams/by-code/NOTX0003").status_code == 200
    resp = client.post(f"{API}/exams/by-code/NOTX0003/start", json={"student_name": "Асель", "student_id": "STU-3"})
    assert resp.status_code == 200 and resp.json()["status"] == "waiting"
    assert probe.in_transaction == [False, False, False]  # by-code lesson check, start lesson check, start roster check


# ── 4: which rows a save / a submit / an entry lock ───────────────────────────────────────────────────────────
@pytest.fixture
def row_locks(monkeypatch):
    """Every ``Query.with_for_update`` as (model, shared?).  SQLite ignores the clause, so the call is recorded."""
    calls = []
    original = Query.with_for_update

    def spy(self, *args, **kwargs):
        calls.append((self.column_descriptions[0]["entity"].__name__, bool(kwargs.get("read"))))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Query, "with_for_update", spy)
    return calls


def test_progress_locks_only_the_students_own_attempt(client, db, row_locks):
    exam = _open_exam(db, "LOCK0001")
    session = make_session_in_db(db, exam)
    resp = client.post(f"{API}/exam-sessions/{session.id}/progress", json={"answers": {"TASK_001": "3"}})
    assert resp.status_code == 200 and resp.json() == {"ok": True, "answered_count": 1}
    assert row_locks == [("ExamSession", False)]


def test_submit_locks_only_the_students_own_attempt(client, db, row_locks):
    exam = _open_exam(db, "LOCK0002")
    session = make_session_in_db(db, exam)
    resp = client.post(f"{API}/exam-sessions/{session.id}/submit", json={"answers": {"TASK_001": "3"}})
    assert resp.status_code == 200
    assert row_locks == [("ExamSession", False)]


def test_polling_and_reading_take_no_lock_while_the_timer_is_running(client, db, row_locks):
    exam = _open_exam(db, "LOCK0003")
    session = make_session_in_db(db, exam)
    assert client.get(f"{API}/exams/by-code/LOCK0003").status_code == 200
    assert client.get(f"{API}/exams/by-code/LOCK0003/session-status/{session.id}").status_code == 200
    assert client.post(f"{API}/exams/by-code/LOCK0003/check-student", json={"student_id": "S1", "student_name": "А"}).status_code == 200
    client.post(f"{API}/exam-sessions/{session.id}/submit", json={"answers": {}})
    row_locks.clear()
    assert client.get(f"{API}/exam-sessions/{session.id}/results").status_code == 200
    assert row_locks == []


def test_starting_takes_only_a_shared_lock_on_the_exam_row(client, db, row_locks):
    _open_exam(db, "LOCK0004")
    resp = client.post(f"{API}/exams/by-code/LOCK0004/start", json={"student_name": "Асель", "student_id": "STU-4"})
    assert resp.status_code == 200
    assert row_locks == [("GeneratedExam", True)]  # FOR SHARE: students entering together do not queue on each other


def test_the_exclusive_exam_lock_is_taken_only_when_the_timer_has_run_out(db, row_locks):
    running = _open_exam(db, "LOCK0005", started_minutes_ago=5)
    assert router_module._check_and_auto_close_exam(db, running) is False
    assert row_locks == []

    running.settings = {**running.settings, "started_at": (datetime.utcnow() - timedelta(minutes=50)).isoformat()}
    db.commit()
    assert router_module._check_and_auto_close_exam(db, running) is True
    assert ("GeneratedExam", False) in row_locks  # exclusive: the finalizer, and only the finalizer
    db.refresh(running)
    assert running.status == "closed"


# ── 5: timeouts are a 503 and retries are safe ────────────────────────────────────────────────────────────────
class _PgError(Exception):
    def __init__(self, pgcode):
        super().__init__(pgcode)
        self.pgcode = pgcode


def _db_error(pgcode, cls=OperationalError):
    return cls("SELECT 1", {}, _PgError(pgcode))


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_db_error("55P03"), LOCK_BUSY_MESSAGE),            # lock_timeout
        (_db_error("40P01"), LOCK_BUSY_MESSAGE),            # deadlock_detected
        (_db_error("57014"), DB_BUSY_MESSAGE),              # statement_timeout
        (_db_error("25P03"), DB_BUSY_MESSAGE),              # idle_in_transaction_session_timeout
        (_db_error(None), DB_BUSY_MESSAGE),                 # a lost connection
        (PoolTimeoutError("QueuePool limit"), DB_BUSY_MESSAGE),
        (ValueError("x"), None),
    ],
)
def test_transient_database_failures_are_recognised(exc, expected):
    assert transient_db_failure(exc) == expected


def test_a_failure_that_is_not_transient_is_not_masked():
    from sqlalchemy.exc import IntegrityError

    assert transient_db_failure(_db_error("23505", IntegrityError)) is None  # a real bug stays a 500


def test_a_lock_timeout_is_a_503_in_russian_and_the_retry_loses_and_doubles_nothing(client, db, monkeypatch):
    exam = _open_exam(db, "RETRY001")
    session = make_session_in_db(db, exam)
    real = router_module._lock_attempt
    attempts = []

    def flaky(db_, session_id):
        attempts.append(session_id)
        if len(attempts) == 1:
            raise _db_error("55P03")  # the first save waited for the row lock and timed out
        return real(db_, session_id)

    monkeypatch.setattr(router_module, "_lock_attempt", flaky)
    url = f"{API}/exam-sessions/{session.id}/progress"
    first = client.post(url, json={"answers": {"TASK_001": "3"}})
    assert first.status_code == 503
    assert first.headers["retry-after"] == "2"
    assert first.json()["detail"] == LOCK_BUSY_MESSAGE and "Подождите" in first.json()["detail"]

    retry = client.post(url, json={"answers": {"TASK_001": "3"}})
    again = client.post(url, json={"answers": {"TASK_001": "3"}})  # and the same retry twice is still one answer
    assert retry.status_code == 200 and again.status_code == 200
    assert retry.json() == again.json() == {"ok": True, "answered_count": 1}
    db.refresh(session)
    assert session.answers == {"TASK_001": "3"}


def test_a_timed_out_submit_graded_nothing_and_its_retry_grades_once(client, db, monkeypatch):
    exam = _open_exam(db, "RETRY002")
    session = make_session_in_db(db, exam)
    real = router_module._lock_attempt
    attempts = []

    def flaky(db_, session_id):
        attempts.append(1)
        if len(attempts) == 1:
            raise _db_error("55P03")
        return real(db_, session_id)

    monkeypatch.setattr(router_module, "_lock_attempt", flaky)
    url = f"{API}/exam-sessions/{session.id}/submit"
    body = {"answers": {"TASK_001": "3", "TASK_002": "5"}, "time_spent_seconds": 60}
    assert client.post(url, json=body).status_code == 503
    db.refresh(session)
    assert session.status == "in_progress" and session.submitted_at is None  # nothing half-written

    ok = client.post(url, json=body)
    assert ok.status_code == 200 and ok.json()["correct_count"] == 2
    db.refresh(session)
    submitted_at, score = session.submitted_at, session.score

    dup = client.post(url, json=body)  # a late duplicate of the retry
    assert dup.status_code == 400 and dup.json()["detail"] == "Экзамен уже сдан"
    db.refresh(session)
    assert (session.submitted_at, session.score, session.answers) == (submitted_at, score, body["answers"])


def test_a_save_after_the_timer_ran_out_is_refused_and_keeps_what_was_saved(client, db):
    exam = _open_exam(db, "RETRY003", started_minutes_ago=50)  # the 45-minute timer is over
    session = make_session_in_db(db, exam)
    session.answers = {"TASK_001": "3"}
    db.commit()
    resp = client.post(f"{API}/exam-sessions/{session.id}/progress", json={"answers": {"TASK_002": "5"}})
    assert resp.status_code == 410
    db.refresh(session)
    db.refresh(exam)
    assert exam.status == "closed" and session.status == "submitted"
    assert session.answers == {"TASK_001": "3"}  # the late answer was not taken; the saved one was graded
    late_submit = client.post(f"{API}/exam-sessions/{session.id}/submit", json={"answers": {"TASK_002": "5"}})
    assert late_submit.status_code == 400  # the finalizer already graded this attempt: «already submitted»
    db.refresh(session)
    assert session.answers == {"TASK_001": "3"}


def test_a_submit_after_the_timer_ran_out_grades_what_was_saved_not_what_arrives_late(client, db):
    exam = _open_exam(db, "RETRY005", started_minutes_ago=50)
    session = make_session_in_db(db, exam)
    session.answers = {"TASK_001": "3"}
    db.commit()
    resp = client.post(f"{API}/exam-sessions/{session.id}/submit", json={"answers": {"TASK_001": "3", "TASK_002": "5"}})
    assert resp.status_code == 410
    db.refresh(session)
    assert session.status == "submitted" and session.answers == {"TASK_001": "3"} and session.correct_count == 1


def test_a_second_submit_of_a_graded_attempt_is_refused_before_the_exam_is_even_read(client, db, monkeypatch):
    exam = _open_exam(db, "RETRY004")
    session = make_session_in_db(db, exam)
    assert client.post(f"{API}/exam-sessions/{session.id}/submit", json={"answers": {"TASK_001": "3"}}).status_code == 200
    monkeypatch.setattr(router_module, "_exam_after_attempt_lock", lambda *a, **k: pytest.fail("exam read"))
    assert client.post(f"{API}/exam-sessions/{session.id}/submit", json={"answers": {}}).status_code == 400


# ── engine safety nets and the thread pool ────────────────────────────────────────────────────────────────────
def test_the_engine_sets_the_server_side_timeouts_and_a_pool_sized_for_the_threads(monkeypatch):
    from src.core.config import Settings

    recorded = {}
    monkeypatch.setattr(database, "create_engine", lambda url, **kwargs: recorded.update(url=url, **kwargs))
    monkeypatch.setattr(
        database, "get_settings", lambda: Settings(database_url="postgresql://u:p@db:5432/exam", app_env="testing")
    )
    database._make_engine()
    options = recorded["connect_args"]["options"]
    assert "-c lock_timeout=5000" in options
    assert "-c statement_timeout=15000" in options
    assert "-c idle_in_transaction_session_timeout=30000" in options
    assert recorded["pool_size"] + recorded["max_overflow"] == 30
    assert recorded["pool_timeout"] == 10 and recorded["pool_pre_ping"] is True


def test_the_lifespan_sizes_the_request_thread_pool():
    from src.core.config import get_settings
    from src.main import app, lifespan

    async def go():
        async with lifespan(app):
            return anyio.to_thread.current_default_thread_limiter().total_tokens

    assert asyncio.run(go()) == get_settings().worker_threads


# ── 6: health must not depend on the blocked path ─────────────────────────────────────────────────────────────
def test_liveness_is_answered_by_the_event_loop_itself():
    from src.main import create_app

    live = next(r for r in create_app().routes if getattr(r, "path", "") == "/health/live")
    assert inspect.iscoroutinefunction(live.endpoint)


def test_health_answers_within_its_bound_when_the_database_check_hangs(client, monkeypatch):
    import threading

    import src.main as main_module
    from src.core import redis_client

    release = threading.Event()

    class Stuck:
        def connect(self):
            release.wait(5)
            raise RuntimeError("released")

    class Redis:
        def ping(self):
            return True

    monkeypatch.setattr(database, "engine", Stuck())
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: Redis())
    monkeypatch.setattr(main_module, "_PROBE_TIMEOUT_SECONDS", 0.2)
    try:
        started = time.monotonic()
        resp = client.get("/health")
        assert time.monotonic() - started < 1.0
        assert resp.status_code == 503 and resp.json()["database"] == "error"
        assert client.get("/health/live").json() == {"status": "alive"}
    finally:
        release.set()  # let the stuck probe thread finish (and log) while the test is still running
        time.sleep(0.2)
