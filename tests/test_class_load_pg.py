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


def test_a_class_of_60_behind_one_nat_joins_within_a_second_without_a_single_429():
    """#33's volume cap must not turn the last students of a real class away (it was 120/min: 60 x check + start)."""
    report = asyncio.run(load.run_load(
        60, PG_URL, REDIS_URL, same_ip=True, ramp=1.0, progress_saves=4, reload_ratio=0.5, request_timeout=15,
        deadline=90,
    ))
    print("\n" + report.render())
    assert report.failures() == []
    assert not any(sample.status == "429" for sample in report.samples)
    assert report.finished_students == 60


@pytest.mark.parametrize("mode,close_after", [("active", 2.0), ("lobby", 1.0)])
def test_a_teacher_watching_the_class_and_closing_the_exam_while_60_students_submit(mode, close_after):
    """The teacher panels (lobby-status, live-summary, sessions) polled the whole time, then «close» under load."""
    report = asyncio.run(load.run_load(
        60, PG_URL, REDIS_URL, mode=mode, teacher=True, close_after=close_after, progress_saves=8, reload_ratio=0.5,
        request_timeout=15, deadline=90,
    ))
    print("\n" + report.render())
    assert report.failures() == []  # teacher answers all 200, no acknowledged save lost, every attempt graded once
    teacher = {name: row for name, row in report.by_endpoint().items() if name.startswith("t-")}
    assert set(teacher) == {"t-close", "t-lobby-status", "t-live-summary", "t-sessions"}
    assert all(row["max"] < 2.0 for row in teacher.values())  # no panel waits for the class


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

            # a submit that races its own retry is graded once, and both are answered with that one result
            body = {"answers": {"TASK_000": "0", "TASK_009": "3"}, "time_spent_seconds": 30}
            one, two = await asyncio.gather(
                http.post(f"/exam-sessions/{sid_a}/submit", json=body, headers=auth_a),
                http.post(f"/exam-sessions/{sid_a}/submit", json=body, headers=auth_a),
            )
            assert one.status_code == two.status_code == 200 and one.json() == two.json()
            assert one.json()["answered_count"] == 2 and one.json()["correct_count"] == 1
        blocker.close()
        engine.dispose()


def test_a_flood_of_slow_teacher_polls_does_not_slow_the_students_down():
    """80 concurrent teacher polls, each waiting 3 s for the group lookup, over a running exam of 60 students.

    Before the staff cap and the released connections: students' p95 2.7 s (the polls held the threads and the pool).
    """
    report = asyncio.run(load.run_load(
        60, PG_URL, REDIS_URL, teacher_pollers=80, group_latency_ms=3000, progress_saves=6, request_timeout=20,
        deadline=120,
    ))
    print("\n" + report.render())
    assert report.failures() == []
    assert report.student_p95() < 1.0
    flood = [s for s in report.samples if s.name == "t-flood"]
    assert any(s.status == "200" for s in flood) and any(s.status == "503" for s in flood)  # served up to the cap, shed beyond
    assert max(s.seconds for s in flood if s.status == "503") < 1.0  # the refusal is immediate, not a queue


def test_a_class_of_60_behind_one_nat_with_a_slow_auth_service_gets_in():
    """Lookups in flight are not failures: 60 students, one address, a burst in half a second, auth answers in 0.6-0.9 s."""
    report = asyncio.run(load.run_load(
        60, PG_URL, REDIS_URL, same_ip=True, ramp=0.5, latency_ms=(600, 900), progress_saves=4, reload_ratio=0.5,
        request_timeout=20, deadline=90,
    ))
    print("\n" + report.render())
    assert report.failures() == []
    assert not any(sample.status == "429" for sample in report.samples)
    assert report.finished_students == 60


def test_a_report_whose_model_takes_35_seconds_is_saved_not_lost():
    """The model call ran inside an open read transaction: killed at 30 s, swallowed, answered as «no submitted works»."""
    asyncio.run(_slow_report_scenario())


async def _graded_students(http, count=3):
    """`count` students start and submit through the API (so the exam has submitted works to report on)."""
    for i in range(count):
        body = {"student_name": f"Ученик {i}", "student_id": f"00000000-0000-0000-0000-00000000010{i}"}
        started = await http.post(f"/exams/by-code/{load.CODE}/start", json=body, headers={"X-Real-IP": f"10.8.0.{i + 1}"})
        assert started.status_code == 200, started.text
        token = {"X-Exam-Session-Token": started.json()["session_token"]}
        done = await http.post(
            f"/exam-sessions/{started.json()['session_id']}/submit",
            json={"answers": {"TASK_000": "3", "TASK_001": "x"}, "time_spent_seconds": 20}, headers=token,
        )
        assert done.status_code == 200, done.text


def _reports_in_db():
    engine = sa.create_engine(PG_URL)
    with engine.connect() as conn:
        count = conn.execute(sa.text("SELECT count(*) FROM exam_reports WHERE exam_id = :e"), {"e": load.EXAM_ID}).scalar()
    engine.dispose()
    return count


async def _slow_report_scenario():
    async with load.running_stack(PG_URL, REDIS_URL, stub_env={"STUB_LLM_LAT_MS": "35000"}) as stack:
        teacher = {"Authorization": "Bearer teacher-load"}
        async with httpx.AsyncClient(base_url=stack.base + API, timeout=100) as http:
            await _graded_students(http)
            began = time.monotonic()
            report, health = await asyncio.gather(
                http.post(f"/exams/{load.EXAM_ID}/report", headers=teacher),
                http.get(stack.base + "/health/live"),
            )
            took = time.monotonic() - began
            assert health.status_code == 200
            assert report.status_code == 200, report.text
            assert took > 34, took  # the model really took its 35 s
            payload = report.json()
            assert payload["narrative_generated"] is True and payload["submitted_count"] == 3
            assert "Класс справился" in payload["narrative_ru"]
            again = await http.get(f"/exams/{load.EXAM_ID}/report", headers=teacher)
            assert again.status_code == 200 and again.json()["submitted_count"] == 3  # and it is in the database


def test_a_double_click_on_generate_report_is_two_200s_and_one_report():
    """Both requests saw no report and both generate it: the loser's INSERT used to violate the unique key -> 500."""
    asyncio.run(_double_click_scenario())


async def _double_click_scenario():
    async with load.running_stack(PG_URL, REDIS_URL, stub_env={"STUB_LLM_LAT_MS": "1500"}) as stack:
        teacher = {"Authorization": "Bearer teacher-load"}
        async with httpx.AsyncClient(base_url=stack.base + API, timeout=60) as http:
            await _graded_students(http)
            engine = sa.create_engine(PG_URL, isolation_level="AUTOCOMMIT")
            with engine.connect() as conn:  # widen the window between «no report yet» and the INSERT, to hit it every time
                conn.execute(sa.text(
                    "CREATE OR REPLACE FUNCTION slow_report_insert() RETURNS trigger AS $$ BEGIN PERFORM pg_sleep(0.6);"
                    " RETURN NEW; END $$ LANGUAGE plpgsql"
                ))
                conn.execute(sa.text(
                    "CREATE TRIGGER slow_report BEFORE INSERT ON exam_reports FOR EACH ROW EXECUTE FUNCTION slow_report_insert()"
                ))
            try:
                first, second, third = await asyncio.gather(*(
                    http.post(f"/exams/{load.EXAM_ID}/report", params={"force": "true"}, headers=teacher) for _ in range(3)
                ))
            finally:
                with engine.connect() as conn:
                    conn.execute(sa.text("DROP TRIGGER IF EXISTS slow_report ON exam_reports"))
                engine.dispose()
            assert [first.status_code, second.status_code, third.status_code] == [200, 200, 200], (first.text, second.text)
            assert _reports_in_db() == 1


def test_a_few_slow_reports_cannot_starve_the_dashboards():
    """Reports hold the small heavy cap and no staff slot: beyond it 503 + Retry-After 10, the dashboard stays served."""
    asyncio.run(_heavy_cap_scenario())


async def _heavy_cap_scenario():
    async with load.running_stack(
        PG_URL, REDIS_URL, stub_env={"STUB_LLM_LAT_MS": "4000"}, extra_env={"HEAVY_SLOTS": "1"},
    ) as stack:
        teacher = {"Authorization": "Bearer teacher-load"}
        async with httpx.AsyncClient(base_url=stack.base + API, timeout=60) as http:
            await _graded_students(http)

            async def dashboards():
                await asyncio.sleep(1.0)  # while the reports are thinking
                worst = 0.0
                for _ in range(5):
                    started = time.monotonic()
                    answer = await http.get("/exams/live-summary", headers=teacher)
                    assert answer.status_code == 200, answer.text
                    worst = max(worst, time.monotonic() - started)
                return worst

            results = await asyncio.gather(
                *(http.post(f"/exams/{load.EXAM_ID}/report", params={"force": "true"}, headers=teacher) for _ in range(6)),
                dashboards(),
            )
            reports, worst_dashboard = results[:-1], results[-1]
            statuses = sorted(r.status_code for r in reports)
            assert set(statuses) <= {200, 503} and statuses.count(200) >= 1 and statuses.count(503) >= 2, statuses
            assert all(r.headers["retry-after"] == "10" for r in reports if r.status_code == 503)
            assert worst_dashboard < 1.5  # three seconds-long reports in flight did not take its slot
