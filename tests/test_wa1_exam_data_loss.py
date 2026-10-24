"""Wave A / pack A1 (code audit 24.10): exam data loss and illegal transitions.

One behavioural regression test per audit ID (EX-02/03/05/16/17/18/19/20/21/23/56/57), driven through the HTTP API of the
SQLite suite.  The matrix of the pack: {draft, variants_ready, lobby, active, closed, archived, print_ready} x {archive,
restore, delete forever} x {results, no results} x {online, print}, a double click, and a sweeper that meets one broken
exam among good ones.  The real-Postgres twin of the double click lives in ``test_wa1_pg_double_click.py``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from src import main as main_module
from src.api import router as router_module
from src.models.exam_models import (
    ExamDeletion,
    ExamLifecycleEvent,
    ExamPrintPackage,
    ExamReport,
    ExamSession,
    GeneratedExam,
)
from tests.conftest import (
    TEST_GROUP_ID,
    TEST_ORG_ID,
    TEST_USER_ID,
    make_exam_in_db,
    make_task,
    session_headers,
    session_token_for,
)

API = "/api/v1"
NO_RESULTS_TEXT = "У экзамена есть результаты учеников — его можно только отправить в архив"


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────────────────
def _exam(db, status, *, exam_id="EXAM_WA1", delivery="online", tasks=None):
    exam = make_exam_in_db(db, tasks=tasks or [make_task("T1", answer="3"), make_task("T2", answer="5")], status=status)
    exam.id = exam_id  # make_exam_in_db always creates EXAM_TEST01; the PK is rewritten before anything references it
    exam.delivery_mode = delivery
    db.commit()
    return exam


def _fresh(db, exam_id):
    db.expire_all()
    return db.query(GeneratedExam).filter(GeneratedExam.id == exam_id).first()


def _session(db, exam, status, *, sid=None, answers=None, score=0):
    sid = sid or f"SES_{uuid4().hex[:8]}"
    row = ExamSession(
        id=sid,
        exam_id=exam.id,
        student_name="Ученик",
        variant_number=1,
        answers=answers or {},
        score=score,
        max_score=20,
        status=status,
        session_token=session_token_for(sid),
    )
    db.add(row)
    db.commit()
    return row


def _event(db, exam):
    db.add(ExamLifecycleEvent(id=str(uuid4()), exam_id=exam.id, org_id=TEST_ORG_ID, event_type="exam_created", details={}))
    db.commit()


def _package(db, exam, status):
    db.add(ExamPrintPackage(
        id=str(uuid4()), exam_id=exam.id, org_id=TEST_ORG_ID, version=1, status=status,
        snapshot={"variants": [{"variant_number": 1, "tasks": []}]}, snapshot_sha256="0" * 64, created_by=TEST_USER_ID,
    ))
    db.commit()


# ═══ EX-03: delete forever ═════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("status", ["draft", "variants_ready", "closed", "archived"])
@pytest.mark.parametrize("session_status", ["submitted", "in_progress"])
def test_ex03_an_exam_with_results_is_never_deleted_forever(client, db, status, session_status):
    exam = _exam(db, status)
    _session(db, exam, session_status, answers={"T1": "3"}, score=10)
    _event(db, exam)

    resp = client.delete(f"{API}/exams/{exam.id}?permanent=true")

    assert resp.status_code == 409
    assert resp.json() == {"detail": NO_RESULTS_TEXT, "code": "EXAM_HAS_RESULTS"}
    assert _fresh(db, exam.id) is not None
    assert db.query(ExamSession).filter(ExamSession.exam_id == exam.id).count() == 1
    assert db.query(ExamLifecycleEvent).filter(ExamLifecycleEvent.exam_id == exam.id).count() == 1
    assert db.query(ExamDeletion).count() == 0


@pytest.mark.parametrize("status", ["draft", "variants_ready", "closed"])
def test_ex03_without_results_only_an_archived_exam_can_be_deleted_forever(client, db, status):
    exam = _exam(db, status)
    resp = client.delete(f"{API}/exams/{exam.id}?permanent=true")
    assert resp.status_code == 409 and resp.json()["code"] == "EXAM_NOT_ARCHIVED"
    assert "в архив" in resp.json()["detail"]
    assert _fresh(db, exam.id).status == status
    assert db.query(ExamDeletion).count() == 0


@pytest.mark.parametrize("status", ["lobby", "active", "print_ready"])
def test_ex03_a_running_exam_cannot_be_deleted_or_archived(client, db, status):
    exam = _exam(db, status, delivery="print" if status == "print_ready" else "online")
    for url in (f"{API}/exams/{exam.id}", f"{API}/exams/{exam.id}?permanent=true"):
        assert client.delete(url).status_code == 409
    assert _fresh(db, exam.id).status == status


@pytest.mark.parametrize("delivery", ["online", "print"])
def test_ex03_an_archived_exam_without_results_is_deleted_and_leaves_a_trace(client, db, delivery):
    exam = _exam(db, "archived", delivery=delivery)
    _event(db, exam)
    _session(db, exam, "waiting")  # a lobby entrant is not a result
    db.add(ExamReport(id="REP_WA1", exam_id=exam.id))
    db.commit()
    if delivery == "print":
        _package(db, exam, "voided")

    resp = client.delete(f"{API}/exams/{exam.id}?permanent=true&reason=дубликат")

    assert resp.status_code == 200 and resp.json()["action"] == "deleted_permanently"
    db.expire_all()
    assert db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).count() == 0
    assert db.query(ExamSession).filter(ExamSession.exam_id == exam.id).count() == 0
    assert db.query(ExamLifecycleEvent).filter(ExamLifecycleEvent.exam_id == exam.id).count() == 0  # FK cascade, as before
    trace = db.query(ExamDeletion).one()
    assert (trace.exam_id, trace.org_id, trace.title, trace.deleted_by, trace.reason) == (
        "EXAM_WA1", TEST_ORG_ID, "Тестовый экзамен", TEST_USER_ID, "дубликат",
    )
    assert trace.deleted_at is not None


def test_ex03_the_trace_survives_the_exam_it_describes(db):
    """No foreign key: the row is still there once the exam row is gone (the whole point of the table)."""
    assert ExamDeletion.__table__.foreign_keys == set()
    exam = _exam(db, "archived")
    db.add(ExamDeletion(id="DEL1", exam_id=exam.id, org_id=TEST_ORG_ID, title=exam.title, deleted_by=TEST_USER_ID))
    db.commit()
    db.delete(exam)
    db.commit()
    assert db.query(ExamDeletion).filter(ExamDeletion.id == "DEL1").count() == 1


def test_ex03_a_deletion_that_fails_leaves_neither_trace_nor_gap(client, db, monkeypatch):
    """The trace row and the removal are one commit: if the removal fails, no orphan trace is left behind."""
    exam = _exam(db, "archived")

    def boom(self, exam_):
        raise RuntimeError("disk full")

    monkeypatch.setattr(router_module.ExamRepository, "delete", boom)
    with pytest.raises(RuntimeError):
        client.delete(f"{API}/exams/{exam.id}?permanent=true")
    db.rollback()
    assert db.query(ExamDeletion).count() == 0


@pytest.mark.parametrize("status", ["draft", "variants_ready", "closed"])
def test_ex03_archiving_keeps_everything_and_a_double_click_does_not_delete(client, db, status):
    exam = _exam(db, status)
    _session(db, exam, "submitted", answers={"T1": "3"}, score=10)

    first = client.delete(f"{API}/exams/{exam.id}")
    second = client.delete(f"{API}/exams/{exam.id}")  # the second click of the same button

    assert first.status_code == 200 and first.json()["action"] == "archived"
    assert second.status_code == 200 and second.json()["action"] == "archived" and second.json()["already_archived"] is True
    assert _fresh(db, exam.id).status == "archived"
    assert db.query(ExamSession).filter(ExamSession.exam_id == exam.id).count() == 1
    assert db.query(ExamDeletion).count() == 0
    events = [e.event_type for e in db.query(ExamLifecycleEvent).filter(ExamLifecycleEvent.exam_id == exam.id)]
    assert events == ["exam_archived"]  # once


# ═══ EX-02 / EX-23: restore ════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("status", ["draft", "variants_ready", "lobby", "print_ready", "active", "closed"])
def test_ex02_only_an_archived_exam_can_be_restored(client, db, status):
    exam = _exam(db, status, delivery="print" if status == "print_ready" else "online")
    exam.access_code = "WA1CODE1" if status in ("lobby", "active") else None
    db.commit()
    session = _session(db, exam, "in_progress", answers={"T1": "3"}) if status == "active" else None

    resp = client.post(f"{API}/exams/{exam.id}/restore")

    assert resp.status_code == 409 and resp.json()["code"] == "EXAM_NOT_ARCHIVED"
    stored = _fresh(db, exam.id)
    assert stored.status == status and stored.access_code == exam.access_code
    if session:
        assert db.query(ExamSession).filter(ExamSession.id == session.id).one().status == "in_progress"
    assert db.query(ExamLifecycleEvent).filter(ExamLifecycleEvent.event_type == "exam_restored").count() == 0


def test_ex02_a_held_online_exam_is_restored_as_closed(client, db):
    exam = _exam(db, "archived")
    _session(db, exam, "submitted", score=10)
    resp = client.post(f"{API}/exams/{exam.id}/restore")
    assert resp.status_code == 200 and resp.json()["status"] == "closed"
    assert _fresh(db, exam.id).status == "closed"


def test_ex23_an_online_exam_without_attempts_comes_back_as_a_clean_draft(client, db):
    exam = _exam(db, "archived")
    exam.access_code, exam.scheduled_lesson_id = "STALE001", "LSN_OLD"
    exam.settings = {**exam.settings, "started_at": datetime.utcnow().isoformat()}
    db.commit()

    resp = client.post(f"{API}/exams/{exam.id}/restore")

    assert resp.status_code == 200 and resp.json()["status"] == "variants_ready"
    stored = _fresh(db, exam.id)
    assert (stored.access_code, stored.scheduled_lesson_id, stored.scheduled_lesson_date) == (None, None, None)
    assert "started_at" not in stored.settings


def test_ex23_a_completed_print_exam_is_restored_as_closed_not_into_a_dead_end(client, db):
    exam = _exam(db, "archived", delivery="print")
    _package(db, exam, "completed")

    resp = client.post(f"{API}/exams/{exam.id}/restore")

    assert resp.status_code == 200 and resp.json()["status"] == "closed"
    # the way on from `closed` works: the copy for the next lesson
    clone = client.post(f"{API}/exams/{exam.id}/clone", json={})
    assert clone.status_code == 200, clone.text


def test_ex23_a_print_draft_with_a_voided_package_is_restored_to_an_editable_state(client, db):
    exam = _exam(db, "archived", delivery="print")
    _package(db, exam, "voided")
    resp = client.post(f"{API}/exams/{exam.id}/restore")
    assert resp.status_code == 200 and resp.json()["status"] == "variants_ready"
    assert client.get(f"{API}/exams/{exam.id}/draft").status_code == 200


def test_ex23_restore_then_archive_then_delete_is_a_consistent_chain(client, db):
    exam = _exam(db, "archived")
    assert client.post(f"{API}/exams/{exam.id}/restore").status_code == 200
    assert client.post(f"{API}/exams/{exam.id}/restore").status_code == 409  # the double click on «restore»
    assert client.delete(f"{API}/exams/{exam.id}?permanent=true").status_code == 409  # not archived any more
    assert client.delete(f"{API}/exams/{exam.id}").json()["action"] == "archived"
    assert client.delete(f"{API}/exams/{exam.id}?permanent=true").status_code == 200


# ═══ EX-05 / EX-16: replace-task ═══════════════════════════════════════════════════════════════════════════════
def _content(task_id):
    task = make_task(task_id, skill_id="SK_2")
    task["id"] = task_id
    return task


@pytest.fixture
def replace(client, db):
    exam = _exam(db, "variants_ready", tasks=[make_task("T1", answer="3"), make_task("T2", answer="5")])

    def call(**body):
        return client.post(f"{API}/exams/{exam.id}/draft/replace-task", json={"variant_number": 1, **body})

    return exam, call


def test_ex05_replacing_with_a_task_already_in_the_variant_is_a_409_and_changes_nothing(replace, db):
    exam, call = replace
    with patch("src.api.router.TaskRepository") as repo:
        repo.return_value.get_task_by_id.side_effect = lambda task_id: _content(task_id)
        resp = call(task_id="T1", old_task_id="T1", new_task_id="T2")
    assert resp.status_code == 409 and "уже есть в варианте" in resp.json()["detail"]
    assert [t["task_id"] for t in _fresh(db, exam.id).variants[0]["tasks"]] == ["T1", "T2"]


def test_ex05_a_new_task_is_accepted_and_replacing_by_itself_is_a_harmless_noop(replace, db):
    exam, call = replace
    with patch("src.api.router.TaskRepository") as repo:
        repo.return_value.get_task_by_id.side_effect = lambda task_id: _content(task_id)
        assert call(task_id="T1", old_task_id="T1", new_task_id="T3").status_code == 200
        assert call(task_id="T2", old_task_id="T2", new_task_id="T2").status_code == 200
    assert [t["task_id"] for t in _fresh(db, exam.id).variants[0]["tasks"]] == ["T3", "T2"]


def test_ex05_a_print_exam_does_not_end_in_a_500(client, db):
    exam = _exam(db, "variants_ready", delivery="print", tasks=[make_task("T1", answer="3"), make_task("T2", answer="5")])
    with patch("src.api.router.TaskRepository") as repo:
        repo.return_value.get_task_by_id.side_effect = lambda task_id: _content(task_id)
        resp = client.post(
            f"{API}/exams/{exam.id}/draft/replace-task",
            json={"variant_number": 1, "task_id": "T1", "old_task_id": "T1", "new_task_id": "T2"},
        )
    assert resp.status_code == 409


def test_ex16_no_alternative_for_a_replacement_is_a_400_not_a_500(replace, db):
    exam, call = replace
    exam.toc_ids_used = [5]  # the attribute the broken branch was meant to read
    db.commit()
    with patch("src.api.router.TaskRepository") as repo:
        repo.return_value.get_tasks_by_skills.return_value = []
        repo.return_value.get_tasks_by_toc_ids.return_value = []
        resp = call(task_id="T1", old_task_id="T1")
    assert resp.status_code == 400 and resp.json()["detail"] == "Нет доступных задач для замены"


def test_ex16_the_exam_wide_chapters_are_the_last_resort_for_an_alternative(replace, db):
    exam, call = replace
    exam.toc_ids_used = [5, 6]
    db.commit()
    seen = {}

    def by_toc(toc_ids, difficulty=None, limit=50, exclude_ids=None):
        seen["toc_ids"], seen["exclude"] = list(toc_ids), list(exclude_ids)
        return [_content("T9")]

    with patch("src.api.router.TaskRepository") as repo:
        repo.return_value.get_tasks_by_skills.return_value = []
        repo.return_value.get_tasks_by_toc_ids.side_effect = by_toc
        resp = call(task_id="T1", old_task_id="T1")
    assert resp.status_code == 200 and resp.json()["new_task"]["task_id"] == "T9"
    assert seen == {"toc_ids": [5, 6], "exclude": ["T1", "T2"]}


# ═══ EX-17 / EX-57: submit keeps autosaved answers; empty answers are not answers ══════════════════════════════
def _live(db):
    exam = _exam(db, "active", exam_id="EXAM_LIVE", tasks=[make_task("T1", answer="3"), make_task("T2", answer="5")])
    exam.access_code = "LIVE0001"
    exam.settings = {**exam.settings, "started_at": (datetime.utcnow() - timedelta(minutes=5)).isoformat()}
    db.commit()
    return exam


def test_ex17_submit_does_not_lose_answers_autosaved_earlier(client, db):
    exam = _live(db)
    session = _session(db, exam, "in_progress", sid="SES_LIVE", answers={"T1": "3"})  # autosaved by `progress`
    resp = client.post(f"{API}/exam-sessions/{session.id}/submit", json={"answers": {"T2": "5"}}, headers=session_headers(session.id))
    assert resp.status_code == 200
    body = resp.json()
    assert (body["score"], body["correct_count"], body["answered_count"]) == (20, 2, 2)
    stored = db.query(ExamSession).filter(ExamSession.id == session.id).one()
    db.refresh(stored)
    assert stored.answers == {"T1": "3", "T2": "5"} and stored.score == 20


def test_ex17_the_request_wins_where_it_speaks_even_to_clear_an_answer(client, db):
    exam = _live(db)
    session = _session(db, exam, "in_progress", sid="SES_LIVE", answers={"T1": "3", "T2": "5"})
    resp = client.post(f"{API}/exam-sessions/{session.id}/submit", json={"answers": {"T1": "7", "T2": ""}}, headers=session_headers(session.id))
    body = resp.json()
    assert (body["score"], body["answered_count"]) == (0, 1)
    db.expire_all()
    assert db.query(ExamSession).one().answers == {"T1": "7", "T2": ""}


def test_ex57_a_cleared_field_is_not_an_answered_task(client, db):
    exam = _live(db)
    session = _session(db, exam, "in_progress", sid="SES_LIVE")
    headers = session_headers(session.id)
    saved = client.post(f"{API}/exam-sessions/{session.id}/progress", json={"answers": {"T1": "3", "T2": "  "}}, headers=headers)
    assert saved.json() == {"ok": True, "answered_count": 1}
    assert client.post(f"{API}/exam-sessions/{session.id}/progress", json={"answers": {"T1": ""}}, headers=headers).json()["answered_count"] == 0
    lobby = client.get(f"{API}/exams/{exam.id}/lobby-status")
    assert [s["answered_count"] for s in lobby.json()["students"]] == [0]
    submitted = client.post(f"{API}/exam-sessions/{session.id}/submit", json={"answers": {"T1": "3", "T2": ""}}, headers=headers)
    assert submitted.json()["answered_count"] == 1
    assert client.get(f"{API}/exam-sessions/{session.id}/results", headers=headers).status_code == 200  # stored count path too


# ═══ EX-18: a non-ASCII token is a 403, never a 500 ════════════════════════════════════════════════════════════
@pytest.mark.parametrize("path,method", [("progress", "post"), ("submit", "post"), ("results", "get")])
def test_ex18_a_non_ascii_session_token_is_refused_with_403(client, db, path, method):
    exam = _live(db)
    session = _session(db, exam, "in_progress", sid="SES_LIVE")
    kwargs = {"json": {"answers": {}}} if method == "post" else {}
    latin1_header = [(b"x-exam-session-token", "é".encode("latin-1"))]  # what a real client may send; Starlette decodes latin-1
    resp = getattr(client, method)(f"{API}/exam-sessions/{session.id}/{path}", headers=latin1_header, **kwargs)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Нет доступа к попытке экзамена"


def test_ex18_the_comparison_is_still_exact():
    ok = router_module._tokens_match
    assert ok("abc", "abc") and not ok("abc", "abd") and not ok("abc", "é") and not ok("é", "abc") and ok("é", "é")
    assert not ok("abc", "abcd")


def test_ex18_a_non_ascii_internal_service_token_is_a_403(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(router_module, "get_settings", lambda: type("S", (), {"internal_service_token": "secret-token"})())
    with pytest.raises(HTTPException) as caught:
        router_module._require_internal_service_token("é-токен")
    assert caught.value.status_code == 403
    router_module._require_internal_service_token("secret-token")  # the right token still passes


# ═══ EX-21: the PDF of a variant that does not exist ═══════════════════════════════════════════════════════════
@pytest.mark.parametrize("kind", ["student", "teacher-key"])
@pytest.mark.parametrize("number", [0, 9, 999999])
def test_ex21_a_missing_variant_pdf_is_a_404_before_any_rendering(client, db, monkeypatch, kind, number):
    exam = _exam(db, "closed", delivery="print")
    _package(db, exam, "completed")
    rendered = []
    for name in ("render_student_exam_pdf", "render_teacher_key_pdf"):
        monkeypatch.setattr(router_module, name, lambda *a, **k: rendered.append(a) or b"%PDF")
    resp = client.get(f"{API}/exams/{exam.id}/print-package/{kind}/{number}.pdf")
    assert resp.status_code == 404 and "не найден" in resp.json()["detail"]
    assert rendered == []
    assert client.get(f"{API}/exams/{exam.id}/print-package/{kind}/1.pdf").status_code == 200  # the existing one is unharmed


def test_ex21_the_draft_preview_of_a_missing_variant_is_a_404(client, db):
    exam = _exam(db, "variants_ready", delivery="print")
    resp = client.get(f"{API}/exams/{exam.id}/print-preview/student/7.pdf")
    assert resp.status_code == 404


# ═══ EX-19: the sweeper meets one broken exam among good ones ══════════════════════════════════════════════════
@pytest.fixture
def real_sessions(tmp_path, monkeypatch):
    """A file database with REAL commits and rollbacks (the shared test session wraps everything in one outer transaction,
    so a rollback there would wipe the data the assertions need)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.models.shared import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'sweep.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(main_module, "SessionLocal", factory)
    yield factory
    engine.dispose()


def _expired(factory, exam_id, *, delivery="online"):
    with factory() as db:
        exam = make_exam_in_db(db, tasks=[make_task("T1", answer="3")], status="active")
        exam.id, exam.delivery_mode = exam_id, delivery
        exam.access_code = exam_id[-8:].upper()
        exam.settings = {**exam.settings, "started_at": (datetime.utcnow() - timedelta(minutes=90)).isoformat()}
        db.commit()


def _status(factory, exam_id):
    with factory() as db:
        return db.query(GeneratedExam).filter(GeneratedExam.id == exam_id).one().status


def test_ex19_one_broken_exam_does_not_stop_the_others_from_closing(real_sessions, caplog):
    _expired(real_sessions, "EXAM_WA1_GOODA")
    _expired(real_sessions, "EXAM_WA1_BROKEN", delivery="print")  # a print exam WITHOUT its package: finalizing raises 404
    _expired(real_sessions, "EXAM_WA1_GOODB")

    with caplog.at_level(logging.ERROR):
        main_module._close_expired_exams_once()

    assert _status(real_sessions, "EXAM_WA1_GOODA") == "closed"
    assert _status(real_sessions, "EXAM_WA1_GOODB") == "closed"
    assert _status(real_sessions, "EXAM_WA1_BROKEN") == "active"  # rolled back whole, retried by the next sweep
    assert any("EXAM_WA1_BROKEN" in record.getMessage() for record in caplog.records)  # named in the log


def test_ex19_the_broken_exam_in_the_middle_does_not_undo_the_ones_before_it(real_sessions):
    for exam_id in ("EXAM_WA1_A1", "EXAM_WA1_A2", "EXAM_WA1_A3"):
        _expired(real_sessions, exam_id)
    _expired(real_sessions, "EXAM_WA1_BAD", delivery="print")
    _expired(real_sessions, "EXAM_WA1_Z1")
    main_module._close_expired_exams_once()
    assert [_status(real_sessions, i) for i in ("EXAM_WA1_A1", "EXAM_WA1_A2", "EXAM_WA1_A3", "EXAM_WA1_Z1")] == ["closed"] * 4


# ═══ EX-20: status changes under the row lock ══════════════════════════════════════════════════════════════════
@pytest.fixture
def locks(monkeypatch):
    from sqlalchemy.orm import Query

    calls, original = [], Query.with_for_update

    def spy(self, *args, **kwargs):
        calls.append(self.column_descriptions[0]["entity"].__name__)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Query, "with_for_update", spy)
    return calls


def test_ex20_start_live_locks_the_exam_row_and_a_second_start_is_a_409(client, db, locks):
    exam = _exam(db, "lobby")
    exam.access_code = "LOBBY001"
    db.commit()
    first = client.post(f"{API}/exams/{exam.id}/start-live")
    started_at = _fresh(db, exam.id).settings["started_at"]
    second = client.post(f"{API}/exams/{exam.id}/start-live")  # the double click
    assert first.status_code == 200 and second.status_code == 409
    assert "GeneratedExam" in locks
    assert _fresh(db, exam.id).settings["started_at"] == started_at  # the common timer was not shifted


def test_ex20_the_state_is_rechecked_after_the_lock_not_before(client, db, monkeypatch):
    """A twin request that won the race while this one awaited the network: the lock re-reads the row and refuses."""
    exam = _exam(db, "lobby")

    async def twin_started_meanwhile(exam_view, actor):
        db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).update({"status": "active"})
        db.commit()

    monkeypatch.setattr(router_module, "_require_lesson_in_progress", twin_started_meanwhile)
    resp = client.post(f"{API}/exams/{exam.id}/start-live")
    assert resp.status_code == 409
    assert _fresh(db, exam.id).settings.get("started_at") is None


def test_ex20_print_start_is_single(client, db, locks, monkeypatch):
    exam = _exam(db, "print_ready", delivery="print")
    _package(db, exam, "ready")
    first = client.post(f"{API}/exams/{exam.id}/print-package/start")
    second = client.post(f"{API}/exams/{exam.id}/print-package/start")
    assert first.status_code == 200 and second.status_code == 409
    assert "GeneratedExam" in locks
    assert db.query(ExamPrintPackage).one().status == "released"


def test_ex20_lobby_transitions_lock_the_exam_row(client, db, locks):
    exam = _exam(db, "variants_ready")
    day = exam.scheduled_lesson_date
    body = {"lesson_id": "LSN_TEST01", "lesson_date": day}
    assert client.post(f"{API}/exams/{exam.id}/open-lobby", json=body).status_code == 200
    assert client.post(f"{API}/exams/{exam.id}/publish", json=body).status_code == 200
    assert client.post(f"{API}/exams/{exam.id}/assign", json={"group_id": TEST_GROUP_ID, "publish": True, **body}).status_code == 200
    assert locks.count("GeneratedExam") == 3


def test_ex20_a_late_rebind_of_an_exam_that_has_started_is_refused(client, db):
    exam = _exam(db, "active")
    body = {"lesson_id": "LSN_TEST01", "lesson_date": exam.scheduled_lesson_date}
    for path, payload in (("open-lobby", body), ("publish", body), ("assign", {"group_id": TEST_GROUP_ID, "publish": True, **body})):
        assert client.post(f"{API}/exams/{exam.id}/{path}", json=payload).status_code == 409, path
    assert _fresh(db, exam.id).status == "active"


def test_ex20_two_prepares_name_the_real_reason(client, db):
    exam = _exam(db, "variants_ready", delivery="print")
    body = {"group_id": TEST_GROUP_ID, "lesson_id": "LSN_TEST01", "lesson_date": exam.scheduled_lesson_date}
    first = client.post(f"{API}/exams/{exam.id}/print-package/prepare", json=body)
    second = client.post(f"{API}/exams/{exam.id}/print-package/prepare", json=body)
    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert "другой открытый экзамен" not in second.json()["detail"]
    assert db.query(ExamPrintPackage).filter(ExamPrintPackage.exam_id == exam.id).count() == 1


def test_ex20_an_integrity_error_names_the_constraint_that_was_hit():
    def err(text):
        return IntegrityError("INSERT", {}, Exception(text))

    code = router_module._binding_conflict(err('duplicate key value violates unique constraint "generated_exams_access_code_key"'))
    lesson = router_module._binding_conflict(err('duplicate key value violates unique constraint "uq_generated_exams_lesson"'))
    package = router_module._binding_conflict(
        err('duplicate key value violates unique constraint "ix_exam_print_packages_exam_id"'), package_exists_message=True
    )
    assert "код доступа" in code.detail
    assert "другой открытый экзамен" in lesson.detail
    assert "комплект" in package.detail


# ═══ EX-56: ids and codes survive a collision ══════════════════════════════════════════════════════════════════
def test_ex56_exam_ids_carry_64_random_bits_and_fit_the_column():
    ids = {router_module._new_exam_id() for _ in range(500)}
    assert len(ids) == 500
    sample = next(iter(ids))
    assert sample.startswith("EXAM_") and len(sample) == 21 <= GeneratedExam.__table__.c.id.type.length
    int(sample[5:], 16)


def test_ex56_an_id_collision_on_clone_draws_a_new_id_instead_of_a_500(client, db, monkeypatch):
    source = _exam(db, "variants_ready")
    draws = iter([source.id, source.id, "EXAM_FRESH_ID_0001"])  # the first two draws collide with an existing exam
    monkeypatch.setattr(router_module, "_new_exam_id", lambda: next(draws))
    resp = client.post(f"{API}/exams/{source.id}/clone", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["exam_id"] == "EXAM_FRESH_ID_0001"
    assert db.query(GeneratedExam).filter(GeneratedExam.id.in_([source.id, "EXAM_FRESH_ID_0001"])).count() == 2
    assert db.query(ExamLifecycleEvent).filter(ExamLifecycleEvent.exam_id == "EXAM_FRESH_ID_0001").count() == 1


def test_ex56_an_exam_id_that_keeps_colliding_gives_up_after_a_bounded_number_of_draws(db, monkeypatch):
    existing = _exam(db, "draft")
    monkeypatch.setattr(router_module, "_new_exam_id", lambda: existing.id)
    clone = GeneratedExam(id=existing.id, title="x", variants=[], settings={}, status="draft", org_id=TEST_ORG_ID)
    with pytest.raises(IntegrityError):
        router_module._add_new_exam(db, clone, attempts=3)
    db.rollback()


def test_ex56_a_taken_access_code_is_never_handed_out_again(db, monkeypatch):
    holder = _exam(db, "lobby")
    holder.access_code = "EXTAKEN01"
    db.commit()
    codes = iter(["EXTAKEN01", "EXTAKEN01", "EXFRESH02"])
    monkeypatch.setattr(router_module.random, "choices", lambda chars, k: list(next(codes)[2:]))
    assert router_module._generate_access_code(db) == "EXFRESH02"


def test_ex56_publishing_gives_a_code_that_is_not_in_use(client, db, monkeypatch):
    holder = _exam(db, "lobby", exam_id="EXAM_HOLDER")
    holder.access_code = "EXTAKEN01"
    holder.scheduled_lesson_id = "LSN_OTHER"
    db.commit()
    mine = _exam(db, "variants_ready", exam_id="EXAM_MINE")
    codes = iter(["EXTAKEN01", "EXFRESH02"])
    monkeypatch.setattr(router_module.random, "choices", lambda chars, k: list(next(codes)[2:]))
    body = {"lesson_id": "LSN_TEST01", "lesson_date": mine.scheduled_lesson_date}
    resp = client.post(f"{API}/exams/{mine.id}/open-lobby", json=body)
    assert resp.status_code == 200 and resp.json()["access_code"] == "EXFRESH02"



# ═══ fix round after R3 ═══════════════════════════════════════════════════════════════════════════════════════
def test_round_refusals_roll_back_so_the_row_lock_is_released(client, db, monkeypatch):
    """A refusal changed nothing: it ends the transaction with a ROLLBACK (which also drops the row lock)."""
    ended = []
    real_rollback, real_commit = db.rollback, db.commit
    monkeypatch.setattr(db, "rollback", lambda: (ended.append("rollback"), real_rollback())[1])
    monkeypatch.setattr(db, "commit", lambda: (ended.append("commit"), real_commit())[1])

    results = _exam(db, "archived", exam_id="EXAM_R1")
    _session(db, results, "submitted")
    running = _exam(db, "variants_ready", exam_id="EXAM_R2")
    twice = _exam(db, "archived", exam_id="EXAM_R3")
    last = []  # how each request ended its transaction (earlier entries are the bridge ending its read transaction)
    assert client.delete(f"{API}/exams/{results.id}?permanent=true").status_code == 409
    last.append(ended[-1])
    assert client.post(f"{API}/exams/{running.id}/restore").status_code == 409
    last.append(ended[-1])
    assert client.delete(f"{API}/exams/{twice.id}").json()["already_archived"] is True
    last.append(ended[-1])
    assert last == ["rollback"] * 3


def _bound(exam):
    return {"lesson_id": "LSN_TEST01", "lesson_date": exam.scheduled_lesson_date}


def test_round_prepare_refuses_a_draft_that_changed_while_the_snapshot_was_built(client, db, monkeypatch):
    exam = _exam(db, "variants_ready", delivery="print")
    body = {"group_id": TEST_GROUP_ID, **_bound(exam)}

    async def replaced_meanwhile(snapshot):  # a concurrent replace_task commits while the figures are fetched
        variants = [{"variant_number": 1, "tasks": [make_task("T1", answer="3"), make_task("T9", answer="1")]}]
        db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).update({"variants": variants})
        db.commit()
        return snapshot

    monkeypatch.setattr(router_module, "_embed_print_figures", replaced_meanwhile)
    resp = client.post(f"{API}/exams/{exam.id}/print-package/prepare", json=body)
    assert resp.status_code == 409 and resp.json()["detail"] == "Черновик изменился, повторите"
    assert db.query(ExamPrintPackage).count() == 0
    assert _fresh(db, exam.id).status == "variants_ready"


def test_round_assign_repeats_the_lobby_rule_on_the_locked_row(client, db, monkeypatch):
    exam = _exam(db, "variants_ready")
    real = router_module.require_group_access

    async def lobby_meanwhile(actor, group_id):  # a twin request opened the lobby while this one asked auth-service
        db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).update({"status": "lobby", "access_code": "TWIN0001"})
        db.commit()
        return await real(actor, group_id)

    monkeypatch.setattr(router_module, "require_group_access", lobby_meanwhile)
    resp = client.post(f"{API}/exams/{exam.id}/assign", json={"group_id": TEST_GROUP_ID, "publish": False})
    assert resp.status_code == 409 and "подтвердите перенос" in resp.json()["detail"]
    assert _fresh(db, exam.id).access_code == "TWIN0001"  # the twin's lobby was not torn down by the unpublished assign


def test_round_open_lobby_refuses_when_the_group_changed_during_the_lookup(client, db, monkeypatch):
    exam = _exam(db, "variants_ready")
    real = router_module._resolve_lesson_occurrence

    async def moved_meanwhile(actor, **kwargs):
        db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).update({"group_id": "99999999-2222-3333-4444-555555555555"})
        db.commit()
        return await real(actor, **kwargs)

    monkeypatch.setattr(router_module, "_resolve_lesson_occurrence", moved_meanwhile)
    resp = client.post(f"{API}/exams/{exam.id}/open-lobby", json=_bound(exam))
    assert resp.status_code == 409 and "Группа экзамена изменилась" in resp.json()["detail"]
    assert _fresh(db, exam.id).status == "variants_ready"


class _Report:
    total_variants, tasks_templatized, tasks_fallback = 2, 1, 1

    class _Variant:
        def to_dict(self):
            return {"variant_number": 1, "tasks": []}

    variants = [_Variant(), _Variant()]


@pytest.mark.parametrize("change", [{"status": "closed"}, {"variants": [{"variant_number": 1, "tasks": [{"task_id": "OTHER"}]}]}])
def test_round_generate_variants_writes_nothing_when_the_exam_changed_meanwhile(client, db, monkeypatch, change):
    exam = _exam(db, "variants_ready")

    class Orchestrator:
        def __init__(self, db):
            pass

        def generate_variants(self, tasks, count, seed):
            db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).update(change)
            db.commit()
            return _Report()

    monkeypatch.setattr(router_module, "VariantOrchestrator", Orchestrator)
    before = _fresh(db, exam.id).variants_count
    resp = client.post(f"{API}/exams/{exam.id}/generate-variants?variants_count=2")
    assert resp.status_code in (400, 409), resp.text
    stored = _fresh(db, exam.id)
    assert stored.variants_count == before and stored.status == change.get("status", "variants_ready")


def test_round_print_start_reads_the_package_state_after_the_lock(client, db, monkeypatch):
    exam = _exam(db, "print_ready", delivery="print")
    _package(db, exam, "ready")

    async def package_voided_meanwhile(exam_view, actor):  # bypasses the session: its identity map still says «ready»
        db.execute(ExamPrintPackage.__table__.update().values(status="voided"))
        db.commit()

    monkeypatch.setattr(router_module, "_require_lesson_in_progress", package_voided_meanwhile)
    resp = client.post(f"{API}/exams/{exam.id}/print-package/start")
    assert resp.status_code == 409
    assert _fresh(db, exam.id).status == "print_ready"


def test_round_the_coded_error_handler_keeps_the_exception_headers():
    import asyncio

    exc = router_module.CodedHTTPException(409, "X_CODE", "text", headers={"Retry-After": "3"})
    response = asyncio.run(main_module.app.exception_handlers[router_module.CodedHTTPException](None, exc))
    assert response.status_code == 409 and response.headers["retry-after"] == "3"
    assert response.body == '{"detail":"text","code":"X_CODE"}'.encode()


# sweeper backoff
@pytest.fixture
def broken_sweep(real_sessions, monkeypatch):
    main_module._sweep_failures.clear()
    now = {"t": 1000.0}
    monkeypatch.setattr(main_module, "_sweep_clock", lambda: now["t"])
    calls = []
    real = main_module._check_and_auto_close_exam

    def counting(db, exam):
        calls.append(exam.id)
        return real(db, exam)

    monkeypatch.setattr(main_module, "_check_and_auto_close_exam", counting)
    _expired(real_sessions, "EXAM_WA1_POISON", delivery="print")
    yield now, calls
    main_module._sweep_failures.clear()


def test_round_a_failing_exam_logs_its_stack_once_and_is_retried_with_backoff(broken_sweep, caplog):
    now, calls = broken_sweep
    with caplog.at_level(logging.WARNING):
        main_module._close_expired_exams_once()
        stacks = [r for r in caplog.records if r.exc_info]
        assert len(stacks) == 1 and "EXAM_WA1_POISON" in stacks[0].getMessage()

        now["t"] += 10  # backing off: not even tried
        main_module._close_expired_exams_once()
        assert calls == ["EXAM_WA1_POISON"]

        now["t"] += 25  # past the first back-off (30 s): tried again, no new stack, no new line yet
        main_module._close_expired_exams_once()
        assert len(calls) == 2 and len([r for r in caplog.records if r.exc_info]) == 1 and len(caplog.records) == 1

        now["t"] += 700  # past the report interval: ONE line, no stack
        main_module._close_expired_exams_once()
        lines = [r for r in caplog.records if not r.exc_info]
        assert len(calls) == 3 and len(lines) == 1 and "still cannot be auto-closed" in lines[0].getMessage()
        assert len([r for r in caplog.records if r.exc_info]) == 1


def test_round_a_recovered_exam_leaves_the_failure_table(broken_sweep, real_sessions):
    now, calls = broken_sweep
    main_module._close_expired_exams_once()
    assert "EXAM_WA1_POISON" in main_module._sweep_failures
    with real_sessions() as db:  # the teacher closes it by hand
        db.query(GeneratedExam).filter(GeneratedExam.id == "EXAM_WA1_POISON").update({"status": "closed"})
        db.commit()
    now["t"] += 1
    main_module._close_expired_exams_once()
    assert main_module._sweep_failures == {}
