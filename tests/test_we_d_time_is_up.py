"""Wave E, E-D (EX-41): the entry (``start``) and the poll (``session-status``) of a running exam say the same when the time is out, and the poll gives a legacy
attempt (in progress, no ``started_at``) its start.  Before: the entry answered «Время экзамена истекло. Ваши сохранённые ответы отправлены на проверку.» with the saved
answers, the poll «Время экзамена истекло» without them - the student read one or the other depending on whether he reloaded the page or waited; a legacy attempt
without a start kept «the full time» on every poll (the clock stood still).  The builder of the tasks is one function (``_build_student_tasks``): the entry and the poll give
the same tasks."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.api import router as router_module
from src.models.exam_models import ExamSession
from tests.conftest import TEST_SESSION_TOKEN, make_exam_in_db, make_session_in_db, make_task

API = "/api/v1"
STATE = {"now": datetime(2026, 11, 3, 10, 0, 0)}
CODE = "WEDTIME1"


class FrozenDatetime(datetime):
    @classmethod
    def utcnow(cls):
        return STATE["now"]


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    STATE["now"] = datetime(2026, 11, 3, 10, 0, 0)
    monkeypatch.setattr(router_module, "datetime", FrozenDatetime)


def _running_exam(db, *, tasks=None):
    exam = make_exam_in_db(db, status="active", tasks=tasks or [make_task("T1", answer="3"), make_task("T2", answer="5")])
    exam.access_code = CODE
    exam.settings = {"time_minutes": 45, "mc_mode": "auto"}          # a legacy row: no ``started_at`` of the exam
    db.commit()
    return exam


def _attempt(db, exam, *, started_minutes_ago, answers=None):
    sess = make_session_in_db(db, exam, status="in_progress")
    sess.student_id = "STU-T"
    sess.answers = answers if answers is not None else {"T1": "3"}
    sess.started_at = STATE["now"] - timedelta(minutes=started_minutes_ago) if started_minutes_ago is not None else None
    db.commit()
    return sess


def _poll(client):
    r = client.get(f"{API}/exams/by-code/{CODE}/session-status/SES_TEST01", headers={"X-Exam-Session-Token": TEST_SESSION_TOKEN})
    assert r.status_code == 200, r.text
    return r.json()


def _enter(client):
    r = client.post(f"{API}/exams/by-code/{CODE}/start", json={"student_name": "Иван Иванов", "student_id": "STU-T"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def timer_is_late(monkeypatch):
    """The common timer of the exam has not closed it yet (the next call after the deadline does): the attempt's own clock is what runs out."""
    monkeypatch.setattr(router_module, "_check_and_auto_close_exam", lambda db, exam: False)


FIELDS = ("status", "already_submitted", "saved_answers", "message")


def test_the_entry_and_the_poll_tell_the_same_when_the_time_is_out(client, db, timer_is_late):
    _attempt(db, _running_exam(db), started_minutes_ago=50, answers={"T1": "3", "T2": "7"})
    polled = _poll(client)
    db.expire_all()
    db.query(ExamSession).update({"status": "in_progress", "score": 0})        # the same attempt, not graded yet: now the student reloads the page
    db.commit()
    entered = _enter(client)
    assert {k: polled[k] for k in FIELDS} == {k: entered[k] for k in FIELDS}
    assert polled["status"] == "submitted" and polled["already_submitted"] is True
    assert polled["saved_answers"] == {"T1": "3", "T2": "7"}                    # the poll used to leave them out
    assert polled["message"] == "Время экзамена истекло. Ваши сохранённые ответы отправлены на проверку."   # ... and to say it shorter


def test_the_poll_grades_the_attempt_once(client, db, timer_is_late):
    _attempt(db, _running_exam(db), started_minutes_ago=50, answers={"T1": "3", "T2": "7"})
    first, second = _poll(client), _poll(client)
    assert first == second
    db.expire_all()
    row = db.get(ExamSession, "SES_TEST01")
    assert row.status == "submitted" and row.correct_count == 1


def test_an_attempt_that_was_removed_meanwhile_is_kicked_not_submitted(client, db, timer_is_late, monkeypatch):
    _attempt(db, _running_exam(db), started_minutes_ago=50)
    monkeypatch.setattr(router_module, "_lock_attempt", lambda db_, session_id: None)
    assert _poll(client) == {"status": "kicked", "message": "Сессия не найдена или удалена преподавателем"}


def test_a_legacy_attempt_gets_its_start_from_the_poll_and_the_clock_runs(client, db):
    _attempt(db, _running_exam(db), started_minutes_ago=None)
    first = _poll(client)
    assert first["status"] == "in_progress" and first["time_remaining_seconds"] == 45 * 60
    db.expire_all()
    assert db.get(ExamSession, "SES_TEST01").started_at == STATE["now"]       # written by the poll (the entry always did)
    STATE["now"] += timedelta(minutes=10)
    second = _poll(client)
    assert second["time_remaining_seconds"] == 35 * 60                        # on the old code: 45 * 60 on every poll


def test_the_entry_and_the_poll_build_the_same_tasks(client, db):
    _attempt(db, _running_exam(db, tasks=[make_task(f"T{i}", answer="3", answer_type="multiple_choice", answer_options=["1", "2", "3", "4"]) for i in range(6)]),
             started_minutes_ago=5)
    polled = _poll(client)
    entered = _enter(client)
    assert polled["tasks"] == entered["tasks"] and len(polled["tasks"]) == 6
    assert polled["time_remaining_seconds"] == entered["time_remaining_seconds"] == 40 * 60
    assert polled["saved_answers"] == entered["saved_answers"]
