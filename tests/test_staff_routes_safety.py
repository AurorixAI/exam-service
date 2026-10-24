"""ROADMAP #95 R3 (F1, F2, F5, F6): the staff side of the same rules, on the SQLite suite.

* F2: a bridged teacher route holds neither a transaction nor a pooled connection while it waits for auth-service
  (``_await(..., db)`` ends the read transaction without expiring what was loaded); a list of exams asks auth-service
  about each DISTINCT group once, concurrently; staff requests have a cap of their own per worker (503 + Retry-After
  when it is full) so a flood of dashboard polls cannot take the threads and connections the students need.
* The dynamic form of the main rule: NO database statement ever runs on the event-loop thread.
* F1: the model/PDF rendering never runs inside an open transaction; a failed save is a 503/500, never «no works».
* F5/F6: ``populate_existing`` under the lock in cancel; nothing pending at a network call in assign; autoflush off.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import threading
import time
from types import SimpleNamespace
from datetime import datetime, timedelta

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Query

from src.api import router as router_module
from src.core import auth as core_auth
from src.core import database
from src.core.auth import StaffSlots
from src.domain import report_generator
from src.models import ExamPrintPackage, ExamReport, ExamSession, GeneratedExam
from src.repositories.exam_repo import ExamReportRepository
from tests.conftest import (
    TEST_GROUP_ID,
    TEST_ORG_ID,
    TEST_USER_ID,
    engine as test_engine,
    make_actor,
    make_exam_in_db,
    make_session_in_db,
    make_task,
)

API = "/api/v1"


def _exam(exam_id, group_id, *, created_by=TEST_USER_ID):
    return GeneratedExam(id=exam_id, org_id=TEST_ORG_ID, group_id=group_id, created_by=created_by, title=exam_id)


# ── F2: staff slots ───────────────────────────────────────────────────────────────────────────────────────────
def test_staff_slot_counter():
    slots = StaffSlots(2)
    assert slots.try_acquire() and slots.try_acquire() and not slots.try_acquire()
    slots.release()
    assert slots.try_acquire() and slots.in_use == 2
    slots.release()
    slots.release()
    slots.release()  # never below zero
    assert slots.in_use == 0


def test_the_last_slots_are_kept_for_requests_that_change_something():
    slots = StaffSlots(4, write_reserve=2)
    assert slots.try_acquire(write=False) and slots.try_acquire(write=False)  # dashboard reads fill two
    assert not slots.try_acquire(write=False)  # the other two are not for reads
    assert slots.try_acquire(write=True) and slots.try_acquire(write=True)  # «close» still gets in
    assert not slots.try_acquire(write=True) and slots.in_use == 4


def _staff_app(monkeypatch, size):
    app = FastAPI()
    gate = {"event": None}
    monkeypatch.setattr(core_auth, "_staff_slots", StaffSlots(size))

    async def resolved(request):
        return make_actor("teacher")

    monkeypatch.setattr(core_auth, "_resolve_staff_actor", resolved)

    @app.get("/hold")
    async def hold(actor=Depends(core_auth.require_exam_staff)):
        await gate["event"].wait()
        return {"ok": True}

    @app.get("/boom")
    async def boom(actor=Depends(core_auth.require_exam_staff)):
        raise RuntimeError("route failed")

    return app, gate


def test_staff_requests_beyond_the_slots_are_refused_at_once_with_503_and_retry_after(monkeypatch):
    import httpx

    app, gate = _staff_app(monkeypatch, 2)

    async def scenario():
        gate["event"] = asyncio.Event()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            running = [asyncio.create_task(http.get("/hold")) for _ in range(2)]
            await asyncio.sleep(0.1)  # both hold a slot
            started = time.monotonic()
            refused = await http.get("/hold")
            took = time.monotonic() - started
            gate["event"].set()
            done = await asyncio.gather(*running)
            again = await http.get("/hold")
        return refused, took, done, again

    refused, took, done, again = asyncio.run(scenario())
    assert refused.status_code == 503 and refused.headers["retry-after"] == "2" and took < 0.5
    assert "преподавател" in refused.json()["detail"]
    assert [r.status_code for r in done] == [200, 200]
    assert again.status_code == 200  # the slots were given back
    assert core_auth.staff_slots().in_use == 0


def test_a_flood_of_reads_cannot_keep_a_write_out(monkeypatch):
    import httpx

    app, gate = _staff_app(monkeypatch, 2)
    monkeypatch.setattr(core_auth, "_staff_slots", StaffSlots(2, write_reserve=1))

    @app.post("/change")
    async def change(actor=Depends(core_auth.require_exam_staff)):
        return {"ok": True}

    async def scenario():
        gate["event"] = asyncio.Event()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            reader = asyncio.create_task(http.get("/hold"))  # holds the one slot reads may use
            await asyncio.sleep(0.1)
            second_read = await http.get("/hold")
            write = await http.post("/change")
            gate["event"].set()
            await reader
        return second_read, write

    second_read, write = asyncio.run(scenario())
    assert second_read.status_code == 503 and write.status_code == 200


def test_a_staff_slot_is_given_back_when_the_route_fails(monkeypatch):
    import httpx

    app, _ = _staff_app(monkeypatch, 1)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            first = await http.get("/boom")
            second = await http.get("/boom")
        return first, second

    first, second = asyncio.run(scenario())
    assert first.status_code == 500 and second.status_code == 500  # the second would be 503 had the slot leaked
    assert core_auth.staff_slots().in_use == 0


def test_the_staff_cap_is_the_setting(monkeypatch):
    from src.core.config import get_settings

    core_auth.reset_staff_slots()
    try:
        assert core_auth.staff_slots().size == get_settings().teacher_slots == 16
        assert get_settings().teacher_write_reserve == 4
    finally:
        core_auth.reset_staff_slots()


# ── F2: the bridge ────────────────────────────────────────────────────────────────────────────────────────────
class _Statements:
    def __init__(self):
        self.calls = []  # (thread ident, first words of the statement)

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        first = statement.split("\n")[0][:60]
        if first.startswith(("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT", "BEGIN")):
            return  # transaction control of the test fixture (savepoint mode), not a query
        self.calls.append((threading.get_ident(), first))


@pytest.fixture
def statements():
    recorder = _Statements()
    event.listen(test_engine, "before_cursor_execute", recorder)
    yield recorder
    event.remove(test_engine, "before_cursor_execute", recorder)


def test_the_bridge_ends_the_read_transaction_without_expiring_what_was_loaded(db, statements):
    exam = make_exam_in_db(db)
    db.commit()

    def in_worker():
        loaded = db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).first()
        assert db.in_transaction()
        before = len(statements.calls)
        result = router_module._await(asyncio.sleep(0, result="done"), db)
        assert result == "done"
        assert not db.in_transaction()  # the pooled connection is free while auth-service is asked
        assert loaded.title == "Тестовый экзамен" and loaded.group_id == TEST_GROUP_ID  # no new query to read it
        assert len(statements.calls) == before
        return True

    assert asyncio.run(run_in_threadpool(in_worker))


def test_a_bridged_call_with_pending_changes_is_refused_in_the_testing_profile(db):
    exam = make_exam_in_db(db)

    def in_worker():
        exam.title = "changed but not flushed"
        router_module._await(asyncio.sleep(0), db)

    with pytest.raises(RuntimeError, match="pending"):
        asyncio.run(run_in_threadpool(in_worker))
    db.refresh(exam)


def test_the_group_lookups_of_a_list_are_one_concurrent_round_per_distinct_group(db, monkeypatch):
    seen = []

    async def lookup(actor, group_id):
        seen.append(group_id)
        await asyncio.sleep(0.3)
        if group_id == "gone":
            raise HTTPException(404, "Группа не найдена")
        if group_id == "other-teacher":
            raise HTTPException(403, "Нет доступа к этой группе")
        return {"id": group_id}

    monkeypatch.setattr(core_auth, "require_group_access", lookup)
    exams = [
        _exam("E1", "g-a"), _exam("E2", "g-a"), _exam("E3", "g-b"), _exam("E4", "gone"), _exam("E5", "other-teacher"),
        _exam("E6", None),  # a library template of the actor: no lookup at all
    ]
    actor = make_actor("teacher")
    started = time.monotonic()
    decisions = asyncio.run(run_in_threadpool(router_module._exam_access_decisions, db, exams, actor))
    took = time.monotonic() - started
    assert sorted(seen) == ["g-a", "g-b", "gone", "other-teacher"]  # once per DISTINCT group
    assert took < 0.9  # concurrently: 4 sequential lookups would take 1.2 s
    assert decisions == {"E1": True, "E2": True, "E3": True, "E4": False, "E5": False, "E6": True}


def test_a_failing_auth_service_is_not_hidden_as_a_missing_group(db, monkeypatch):
    async def lookup(actor, group_id):
        raise HTTPException(503, "Не удалось проверить доступ к группе")

    monkeypatch.setattr(core_auth, "require_group_access", lookup)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(run_in_threadpool(router_module._exam_access_decisions, db, [_exam("E1", "g-a")], make_actor("teacher")))
    assert caught.value.status_code == 503


def test_an_admin_and_a_foreign_exam_need_no_group_lookup(db, monkeypatch):
    async def lookup(actor, group_id):  # pragma: no cover - must not be reached
        raise AssertionError("no lookup expected")

    monkeypatch.setattr(core_auth, "require_group_access", lookup)
    foreign = GeneratedExam(id="X1", org_id="ORG_OTHER", group_id="g-a", created_by="someone", title="x")
    admin = make_actor("admin")
    assert asyncio.run(run_in_threadpool(router_module._exam_access_decisions, db, [_exam("E1", "g-a")], admin)) == {"E1": True}
    assert asyncio.run(run_in_threadpool(router_module._exam_access_decisions, db, [foreign], make_actor("teacher"))) == {"X1": False}


# ── the dynamic form of the main rule ─────────────────────────────────────────────────────────────────────────
def test_no_database_statement_ever_runs_on_the_event_loop_thread(client, db, statements, monkeypatch):
    """Teacher and student routes, one after the other, through the real app: every cursor.execute is in a worker."""
    loop_threads = set()
    original = router_module._run_awaitable

    async def noting(awaitable):
        loop_threads.add(threading.get_ident())
        return await original(awaitable)

    monkeypatch.setattr(router_module, "_run_awaitable", noting)
    exam = make_exam_in_db(db, tasks=[make_task("TASK_001", answer="3")], status="active")
    exam.access_code = "LOOP0001"
    exam.settings = {**exam.settings, "started_at": (datetime.utcnow() - timedelta(minutes=2)).isoformat()}
    db.commit()
    session = make_session_in_db(db, exam)

    assert client.get(f"{API}/exams/{exam.id}/lobby-status").status_code == 200  # teacher: bridged -> loop thread known
    assert loop_threads, "the bridge never ran"
    calls = [
        client.get(f"{API}/exams/live-summary"),
        client.get(f"{API}/exams"),
        client.get(f"{API}/exams/{exam.id}"),
        client.get(f"{API}/exams/{exam.id}/sessions"),
        client.get(f"{API}/exams/{exam.id}/analytics"),
        client.get(f"{API}/exams/by-code/LOOP0001"),
        client.post(f"{API}/exams/by-code/LOOP0001/check-student", json={"student_id": "S1", "student_name": "А"}),
        client.post(f"{API}/exams/by-code/LOOP0001/start", json={"student_name": "Б", "student_id": "S2"}),
        client.post(f"{API}/exam-sessions/{session.id}/progress", json={"answers": {"TASK_001": "3"}}),
        client.post(f"{API}/exam-sessions/{session.id}/submit", json={"answers": {"TASK_001": "3"}}),
        client.get(f"{API}/exam-sessions/{session.id}/results"),
        client.post(f"{API}/exams/{exam.id}/close"),
    ]
    assert [r.status_code for r in calls] == [200] * len(calls), [r.text[:80] for r in calls if r.status_code != 200]
    on_loop = [sql for ident, sql in statements.calls if ident in loop_threads]
    assert on_loop == []
    assert len(statements.calls) > 20


def test_no_orm_change_precedes_a_bridged_network_call():
    """Static form of «nothing pending at a network call»: no attribute assignment, add or delete above an ``_await``."""
    tree = ast.parse(inspect.getsource(router_module))
    offenders = []
    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        bridged = [c.lineno for c in ast.walk(func) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_await"]
        if not bridged:
            continue
        last = max(bridged)
        for node in ast.walk(func):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AugAssign) else []
            changes_orm = any(isinstance(t, ast.Attribute) for t in targets)
            writes = isinstance(node, ast.Call) and getattr(node.func, "attr", "") in {"add", "add_all", "delete", "merge"}
            if (changes_orm or writes) and node.lineno < last:
                offenders.append(f"{func.name}:{node.lineno}")
    assert offenders == []


# ── F5, F6 ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_session_factory_never_autoflushes():
    """The bridge rule «nothing pending at a network call» relies on ORM changes staying in memory until commit."""
    assert database.SessionLocal.kw["autoflush"] is False
    assert database.SessionLocal.kw["autocommit"] is False


def test_assign_changes_nothing_before_the_lesson_lookup(client, db, monkeypatch):
    exam = make_exam_in_db(db, status="variants_ready")
    exam.group_id = None
    exam.group_name = None
    exam.scheduled_lesson_id = None
    exam.scheduled_lesson_date = None
    db.commit()
    seen = {}
    real = router_module._resolve_lesson_occurrence

    async def lookup(actor, **kwargs):
        # at this very moment (the network call) the session holds no change to the exam
        seen["pending"] = bool(db.dirty or db.new or db.deleted)
        seen["group"] = exam.group_id
        return await real(actor, **kwargs)

    monkeypatch.setattr(router_module, "_resolve_lesson_occurrence", lookup)
    resp = client.post(
        f"{API}/exams/{exam.id}/assign",
        json={"group_id": TEST_GROUP_ID, "publish": True, "lesson_id": "LSN_X", "lesson_date": datetime.utcnow().date().isoformat()},
    )
    assert resp.status_code == 200, resp.text
    assert seen == {"pending": False, "group": None}
    db.refresh(exam)
    assert exam.status == "lobby" and exam.group_id == TEST_GROUP_ID and exam.scheduled_lesson_id == "LSN_X"


def test_cancel_re_reads_the_exam_under_its_lock(client, db, monkeypatch):
    exam = make_exam_in_db(db, status="lobby")
    exam.access_code = "CANCEL001"
    db.commit()
    populated = []
    original = Query.populate_existing

    def spy(self):
        populated.append(self.column_descriptions[0]["entity"].__name__)
        return original(self)

    monkeypatch.setattr(Query, "populate_existing", spy)
    resp = client.post(f"{API}/exams/{exam.id}/delivery/cancel", json={"reason": "Урок отменён преподавателем"})
    assert resp.status_code == 200, resp.text
    assert "GeneratedExam" in populated


# ── F1: reports and PDF rendering ─────────────────────────────────────────────────────────────────────────────
def _graded_exam(db, code="REPORT001"):
    exam = make_exam_in_db(db, tasks=[make_task("TASK_001", answer="3")], status="closed")
    exam.access_code = code
    db.commit()
    session = make_session_in_db(db, exam, status="submitted")
    session.answers = {"TASK_001": "3"}
    session.score, session.max_score, session.correct_count, session.total_tasks = 10, 10, 1, 1
    db.commit()
    return exam, session


def test_the_model_is_called_with_no_transaction_open(client, db, monkeypatch):
    exam, _ = _graded_exam(db)
    state = {}

    def model(prompt, **kwargs):
        state["in_transaction"] = db.in_transaction()
        return '{"summary": "Класс справился", "strengths": [], "critical_gaps": []}'

    monkeypatch.setattr(report_generator, "call_gemini", model)
    resp = client.post(f"{API}/exams/{exam.id}/report")
    assert resp.status_code == 200 and resp.json()["narrative_generated"] is True
    assert state == {"in_transaction": False}  # the model call (up to 120 s) never sits inside a transaction
    assert db.query(ExamReport).filter(ExamReport.exam_id == exam.id).count() == 1


def test_the_report_is_final_when_the_exam_was_closed_while_the_model_was_thinking(client, db, monkeypatch):
    exam, _ = _graded_exam(db, "REPORT002")
    exam.status = "active"
    exam.settings = {**exam.settings, "started_at": datetime.utcnow().isoformat()}
    db.commit()

    def model(prompt, **kwargs):
        db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).update({"status": "closed"})
        db.commit()  # the teacher pressed «close» during the call
        return '{"summary": "ok"}'

    monkeypatch.setattr(report_generator, "call_gemini", model)
    resp = client.post(f"{API}/exams/{exam.id}/report")
    assert resp.status_code == 200 and resp.json()["is_final"] is True


def test_a_lost_connection_while_saving_the_report_is_a_503_not_no_submitted_works(client, db, monkeypatch):
    exam, _ = _graded_exam(db, "REPORT003")
    monkeypatch.setattr(report_generator, "call_gemini", lambda *a, **k: '{"summary": "ok"}')

    class Gone(Exception):
        pgcode = "25P03"

    def upsert(self, *args, **kwargs):
        raise OperationalError("INSERT", {}, Gone("terminating connection due to idle-in-transaction timeout"))

    monkeypatch.setattr(router_module.ExamReportRepository, "upsert", upsert)
    resp = client.post(f"{API}/exams/{exam.id}/report")
    assert resp.status_code == 503 and resp.headers["retry-after"] == "2"
    assert "422" not in resp.text and "Нет сданных" not in resp.text


def test_no_submitted_works_is_still_a_422(client, db):
    exam = make_exam_in_db(db, status="closed")
    resp = client.post(f"{API}/exams/{exam.id}/report")
    assert resp.status_code == 422 and "Нет сданных работ" in resp.json()["detail"]


@pytest.mark.parametrize("kind,renderer", [("student", "render_student_exam_pdf"), ("teacher-key", "render_teacher_key_pdf")])
def test_the_print_pdf_is_rendered_with_no_transaction_open(client, db, monkeypatch, kind, renderer):
    exam = make_exam_in_db(db, status="print_ready")
    exam.delivery_mode = "print"
    db.commit()
    db.add(ExamPrintPackage(
        id="PKG_1", exam_id=exam.id, org_id=TEST_ORG_ID, version=1, status="ready", snapshot={"variants": [{"variant_number": 1, "tasks": []}]},
        snapshot_sha256="0" * 64, created_by=TEST_USER_ID,
    ))
    db.commit()
    state = {}

    def render(snapshot, variant_number):
        state["in_transaction"] = db.in_transaction()
        return b"%PDF-stub"

    monkeypatch.setattr(router_module, renderer, render)
    resp = client.get(f"{API}/exams/{exam.id}/print-package/{kind}/1.pdf")
    assert resp.status_code == 200 and resp.content == b"%PDF-stub"
    assert state == {"in_transaction": False}


# ── mini-R3: the long calls have a cap of their own ───────────────────────────────────────────────────────────
def test_long_staff_calls_have_their_own_small_cap_and_hold_no_staff_slot(monkeypatch):
    import httpx

    app, gate = _staff_app(monkeypatch, 1)  # one staff slot, so a leak of it would show at once
    monkeypatch.setattr(core_auth, "_heavy_slots", StaffSlots(1))

    @app.get("/heavy")
    async def heavy(actor=Depends(core_auth.require_heavy_staff)):
        await gate["event"].wait()
        return {"ok": True}

    async def scenario():
        gate["event"] = asyncio.Event()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            first = asyncio.create_task(http.get("/heavy"))
            await asyncio.sleep(0.1)
            in_use = (core_auth.heavy_slots().in_use, core_auth.staff_slots().in_use)
            refused = await http.get("/heavy")
            gate["event"].set()  # the dashboard poll below must not wait for the report to end
            dashboard = await http.get("/hold")
            await first
            again = await http.get("/heavy")
        return in_use, refused, dashboard, again

    in_use, refused, dashboard, again = asyncio.run(scenario())
    assert in_use == (1, 0)  # the long call holds a heavy slot and NO staff slot
    assert refused.status_code == 503 and refused.headers["retry-after"] == "10"
    assert dashboard.status_code == 200 and again.status_code == 200
    assert (core_auth.heavy_slots().in_use, core_auth.staff_slots().in_use) == (0, 0)


def test_only_the_report_and_the_pdf_routes_take_the_heavy_cap():
    heavy = set()
    for route in router_module.router.routes:
        calls = {dep.call for dep in getattr(getattr(route, "dependant", None), "dependencies", [])}
        if core_auth.require_heavy_staff in calls:
            heavy.add(route.endpoint.__name__)
            assert core_auth.require_exam_staff not in calls  # and none of them also holds a staff slot
    assert heavy == {
        "generate_exam_report_endpoint", "download_print_student_pdf", "download_print_teacher_key_pdf", "preview_print_pdf",
    }


def test_the_heavy_cap_is_three_per_worker():
    from src.core.config import get_settings

    core_auth.reset_staff_slots()
    try:
        assert core_auth.heavy_slots().size == get_settings().heavy_slots == 3
    finally:
        core_auth.reset_staff_slots()


# ── mini-R3: a double click on «generate report» ──────────────────────────────────────────────────────────────
def _report_values(**over):
    base = dict(
        total_students=3, submitted_count=3, avg_score_pct=50.0, pass_rate_pct=40.0, task_analytics=[], top_errors=[],
        narrative_ru="first", narrative_generated=True, is_final=False,
    )
    base.update(over)
    return base


def test_the_loser_of_a_report_race_overwrites_the_winners_row_instead_of_failing(db, monkeypatch):
    exam = make_exam_in_db(db)
    repo = ExamReportRepository(db)
    repo.upsert(exam.id, "er_winner", **_report_values(narrative_ru="winner"))

    real = ExamReportRepository.get_by_exam
    calls = {"n": 0}

    def blind_once(self, exam_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(self, exam_id)  # the loser looked before the winner committed

    monkeypatch.setattr(ExamReportRepository, "get_by_exam", blind_once)
    loser = repo.upsert(exam.id, "er_loser", **_report_values(narrative_ru="loser", submitted_count=2, is_final=True))
    assert loser.id == "er_winner" and loser.narrative_ru == "loser" and loser.submitted_count == 2 and loser.is_final
    assert db.query(ExamReport).filter(ExamReport.exam_id == exam.id).count() == 1


def test_a_failure_that_is_not_the_unique_key_is_still_an_error(db, monkeypatch):
    exam = make_exam_in_db(db)
    repo = ExamReportRepository(db)
    with pytest.raises(Exception):
        repo.upsert("NO_SUCH_EXAM", "er_x", **_report_values())  # foreign key: there is no row to fall back to
    assert db.query(ExamReport).filter(ExamReport.exam_id == exam.id).count() == 0


# ── mini-R3: the bridge sees writes that are already flushed ──────────────────────────────────────────────────
def _bridge_in_worker(db, work):
    def in_worker():
        work()
        return router_module._await(asyncio.sleep(0, result="done"), db)

    return asyncio.run(run_in_threadpool(in_worker))


def test_a_flushed_write_blocks_the_bridge(db):
    from src.models import ExamLifecycleEvent

    exam = make_exam_in_db(db)

    def flush_an_event():
        db.add(ExamLifecycleEvent(id="EV_1", exam_id=exam.id, org_id=TEST_ORG_ID, event_type="exam_closed"))
        db.flush()  # nothing is «pending» any more, but the row (and its locks) are in the transaction
        assert not (db.new or db.dirty or db.deleted)
        assert database.wrote_since_commit(db)

    with pytest.raises(RuntimeError, match="written since the last commit"):
        _bridge_in_worker(db, flush_an_event)


def test_a_bulk_update_blocks_the_bridge(db):
    exam = make_exam_in_db(db)

    def bulk_update():
        db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).update({"title": "renamed"})

    with pytest.raises(RuntimeError, match="written since the last commit"):
        _bridge_in_worker(db, bulk_update)


def test_a_bulk_delete_blocks_the_bridge(db):
    exam = make_exam_in_db(db)

    def bulk_delete():
        db.query(ExamReport).filter(ExamReport.exam_id == exam.id).delete()

    with pytest.raises(RuntimeError, match="written since the last commit"):
        _bridge_in_worker(db, bulk_delete)


def test_a_row_lock_blocks_the_bridge(db):
    exam = make_exam_in_db(db)

    def lock_the_row():
        db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).with_for_update().first()

    with pytest.raises(RuntimeError, match="written since the last commit"):
        _bridge_in_worker(db, lock_the_row)


def test_reading_flags_nothing_and_a_commit_clears_the_flag(db):
    exam = make_exam_in_db(db)

    def read_then_write_then_commit():
        db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).first()
        assert not database.wrote_since_commit(db)
        db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).update({"title": "renamed"})
        assert database.wrote_since_commit(db)
        db.commit()
        assert not database.wrote_since_commit(db)

    assert _bridge_in_worker(db, read_then_write_then_commit) == "done"


def test_in_production_a_bridged_call_over_writes_is_logged_and_commits_nothing(db, monkeypatch, caplog):
    exam = make_exam_in_db(db)
    monkeypatch.setattr(router_module, "get_settings", lambda: SimpleNamespace(app_env="production"))
    state = {}

    def write_then_call():
        db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).update({"title": "renamed"})
        with caplog.at_level(logging.ERROR, logger="exam_service"):
            state["result"] = router_module._await(asyncio.sleep(0, result="done"), db)
        state["still_open"] = db.in_transaction()
        state["still_flagged"] = database.wrote_since_commit(db)  # no commit happened: nothing is half-saved

    asyncio.run(run_in_threadpool(write_then_call))
    assert state == {"result": "done", "still_open": True, "still_flagged": True}
    assert any("uncommitted writes or row locks" in record.getMessage() for record in caplog.records)
