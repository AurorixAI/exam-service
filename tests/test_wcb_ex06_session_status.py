"""WC-B EX-06: the poll of the waiting room (``GET /exams/by-code/{code}/session-status/{id}``) no longer reads the task JSON of the exam.

30 students poll every 1.5 s = 20 requests a second, each used to carry every variant of the exam (``variants``, hundreds of KB).  The
poll in the lobby needs only scalar columns; the branch that builds the student's tasks (exam active) reads ``exam.variants`` itself and the
deferred column loads there - the path lobby -> teacher starts -> the student's exam is proved below, answer for answer, against the old query.
The clock of the router is frozen (LESSONS 76/82)."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from src.api import router as router_module
from src.models.exam_models import ExamSession
from src.repositories.exam_repo import ExamRepository
from tests.conftest import TEST_SESSION_TOKEN, make_exam_in_db, make_session_in_db, make_task
from tests.sql_probe import count_statements

API = "/api/v1"
NOW = datetime(2026, 11, 2, 9, 30, 0)


class FrozenDatetime(datetime):
    @classmethod
    def utcnow(cls):
        return cls(NOW.year, NOW.month, NOW.day, NOW.hour, NOW.minute, NOW.second)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(router_module, "datetime", FrozenDatetime)


def big_variants(variants=8, tasks=25):
    """A realistic exam: 8 variants x 25 tasks with options and explanations (~0.3-1 MB in production)."""
    out = []
    for v in range(1, variants + 1):
        ts = []
        for i in range(tasks):
            t = make_task(task_id=f"T{v}_{i}", text="Решите уравнение: " + "x + 1 = 2; " * 12, answer="3", answer_type="multiple_choice",
                          answer_options=["1", "2", "3", "4"])
            t["distractor_meta"] = [{"value": "1", "explanation": "Ученик забыл знак. " * 8}, {"value": "2", "explanation": "Ученик сложил. " * 8}]
            ts.append(t)
        out.append({"variant_number": v, "tasks": ts})
    return out


def fresh_request(db):
    """Every request of production opens its own Session with an empty identity map; the ``db`` fixture is one Session for the whole test."""
    db.commit()
    db.expunge_all()


def lobby_exam(db):
    exam = make_exam_in_db(db, status="lobby")
    exam.variants = big_variants()
    exam.variants_count = 8
    exam.access_code = "WCBLOBBY"
    exam.settings = {"time_minutes": 45, "mc_mode": "auto"}
    db.commit()
    sess = make_session_in_db(db, exam, status="waiting")
    sess.variant_number = 3
    db.commit()
    return exam, sess


def poll(client, exam, sess):
    # the code and the id are constants of the fixture: reading them from an expired ORM object would add a SELECT to the count
    r = client.get(f"{API}/exams/by-code/WCBLOBBY/session-status/SES_TEST01", headers={"X-Exam-Session-Token": TEST_SESSION_TOKEN})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def old_query(monkeypatch):
    """The query of the code before this item: ``without_tasks`` ignored, the task JSON always read."""
    real = ExamRepository.get_by_access_code

    def old(self, code, *, without_tasks=False):
        return real(self, code, without_tasks=False)

    return lambda: monkeypatch.setattr(ExamRepository, "get_by_access_code", old)


def test_the_lobby_poll_reads_no_task_json_and_answers_the_same(client, db, monkeypatch, old_query):
    exam, sess = lobby_exam(db)
    payload = len(json.dumps(exam.variants))
    assert payload > 300_000        # the size of what used to travel with every poll
    fresh_request(db)

    with count_statements() as new:
        after = poll(client, exam, sess)
    fresh_request(db)
    old_query()
    with count_statements() as old:
        before = poll(client, exam, sess)

    assert after == before == {"status": "waiting", "exam_status": "lobby"}
    print(f"\nEX-06 lobby poll: SQL {old.count} -> {new.count}; SELECT of variants {old.reading('generated_exams', 'variants')} -> "
          f"{new.reading('generated_exams', 'variants')}; JSONB per poll {payload} B -> 0 B")
    assert old.reading("generated_exams", "variants") == 1
    assert new.reading("generated_exams", "variants") == 0
    assert new.count == old.count


def test_a_closed_exam_poll_reads_no_task_json_either(client, db, old_query):
    exam, sess = lobby_exam(db)
    exam.status = "closed"
    db.commit()
    fresh_request(db)
    with count_statements() as new:
        after = poll(client, exam, sess)
    assert new.reading("generated_exams", "variants") == 0
    assert after["status"] == "closed"


def test_lobby_to_start_to_exam_the_student_gets_the_same_tasks_and_variants_load_once(client, db, old_query):
    """The only path from the waiting room into the exam: the teacher starts, the next poll of the student returns his tasks."""
    exam, sess = lobby_exam(db)
    assert client.post(f"{API}/exams/{exam.id}/start-live").status_code == 200
    fresh_request(db)

    with count_statements() as new:
        after = poll(client, exam, sess)
    fresh_request(db)
    old_query()
    with count_statements() as old:
        before = poll(client, exam, sess)

    assert after == before
    assert after["status"] == "in_progress" and after["variant_number"] == 3
    assert len(after["tasks"]) == 25 and after["tasks"][0]["task_id"] == "T3_0" and after["tasks"][0]["options"]
    assert after["time_remaining_seconds"] == 45 * 60
    print(f"\nEX-06 first poll after the start: SQL {old.count} -> {new.count}; SELECT of variants "
          f"{old.reading('generated_exams', 'variants')} -> {new.reading('generated_exams', 'variants')} (loaded once, late)")
    assert new.reading("generated_exams", "variants") == 1       # the deferred column is loaded where the tasks are built
    assert old.reading("generated_exams", "variants") >= 1
    assert new.count - old.count <= 1


def test_the_waiting_student_moves_to_in_progress_on_the_poll_of_an_active_exam(client, db, old_query):
    """A session that was still ``waiting`` when the exam became active (the exam flipped by the timer/teacher elsewhere) starts on the poll."""
    exam, sess = lobby_exam(db)
    exam.status = "active"
    exam.settings = {"time_minutes": 45, "mc_mode": "auto", "started_at": NOW.isoformat()}
    db.commit()
    fresh_request(db)
    after = poll(client, exam, sess)
    sess = db.get(ExamSession, "SES_TEST01")
    assert sess.status == "in_progress" and after["status"] == "in_progress" and len(after["tasks"]) == 25
    sess.status = "waiting"
    db.commit()
    fresh_request(db)
    old_query()
    assert poll(client, exam, sess) == after


def test_an_expired_exam_is_still_graded_by_the_poll(client, db):
    """Time ran out while the student reconnected: the poll grades and submits; the grader reads the task JSON through the same exam object."""
    exam, sess = lobby_exam(db)
    exam.status = "active"
    exam.settings = {"time_minutes": 45, "mc_mode": "auto", "started_at": datetime(2026, 11, 2, 8, 0, 0).isoformat()}
    db.commit()
    sess.status = "in_progress"
    sess.answers = {"T3_0": "3", "T3_1": "3", "T3_2": "1"}
    db.commit()
    fresh_request(db)
    body = poll(client, exam, sess)
    assert body["status"] == "submitted" and body["already_submitted"] is True     # the common timer closed the exam first: the saved attempt is a result
    sess = db.get(ExamSession, "SES_TEST01")
    assert sess.status == "submitted" and sess.total_tasks == 25 and sess.correct_count == 2 and sess.score == 20   # graded against the late-loaded variants
