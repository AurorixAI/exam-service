"""WC-B EX-46: the entry of a student (check-student, start, the resume check) reads HIS attempts, not every attempt of the class.

Before: ``list_by_exam`` - all attempts of the exam, every column (the answer JSON and the distractor analytics included), twice per student
(check-student + start); a class of 30 = ~1 800 rows a minute of entry, quadratic in the class.  After: ``list_for_identity`` - the rows whose
``student_id`` or ``display_id`` is one of the student's two identities; ``_find_student_session`` (the one rule of «one student - one attempt»)
still chooses among them.  The «old» numbers below are the same calls with ``_student_sessions`` switched back to ``list_by_exam``.
Cases of the rule: canonical id, display id, a legacy row that kept the display id in ``student_id``, case and spaces, a namesake (same name,
other identity) who must never match, no identity at all."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.api import router as router_module
from src.models.exam_models import ExamSession
from src.repositories.exam_repo import SessionRepository
from tests.conftest import make_exam_in_db
from tests.sql_probe import count_statements

API = "/api/v1"
BASE = datetime(2026, 11, 2, 9, 0, 0)
UUIDS = [f"aaaaaaaa-0000-4000-8000-{i:012d}" for i in range(30)]
DISPLAY = [f"TE-{i:03d}" for i in range(30)]


def seed(db, n=30):
    exam = make_exam_in_db(db, status="lobby")
    exam.access_code = "EX46CODE"
    db.commit()
    heavy = {f"T{i}": "ответ " * 20 for i in range(40)}
    for i in range(n):
        db.add(ExamSession(
            id=f"S{i:03d}", exam_id=exam.id, student_id=UUIDS[i], display_id=DISPLAY[i], student_name=f"Ученик {i}", variant_number=1,
            answers=heavy, distractor_analytics={"x": [1] * 50}, max_score=250, status="submitted" if i % 3 == 0 else "in_progress",
            session_token=f"tok{i}", started_at=BASE + timedelta(seconds=i)))
    db.commit()
    return exam


def ids_of(rows):
    return [s.id for s in rows]


def old_rule(db, exam_id, student_id, identifier, statuses):
    s = router_module._find_student_session(SessionRepository(db).list_by_exam(exam_id), statuses, student_id, identifier)
    return s.id if s else None


def new_rule(db, exam_id, student_id, identifier, statuses):
    s = router_module._find_student_session(router_module._student_sessions(db, exam_id, student_id, identifier), statuses, student_id, identifier)
    return s.id if s else None


ALL = ("submitted",), ("waiting", "in_progress"), ("in_progress",)


def test_every_student_of_the_class_gets_the_same_answer_from_both_queries(db):
    exam = seed(db)
    for i in range(30):
        for statuses in ALL:
            assert new_rule(db, exam.id, UUIDS[i], DISPLAY[i], statuses) == old_rule(db, exam.id, UUIDS[i], DISPLAY[i], statuses)
            assert new_rule(db, exam.id, UUIDS[i], None, statuses) == old_rule(db, exam.id, UUIDS[i], None, statuses)
            assert new_rule(db, exam.id, None, DISPLAY[i], statuses) == old_rule(db, exam.id, None, DISPLAY[i], statuses)
    assert new_rule(db, exam.id, UUIDS[3], DISPLAY[3], ("submitted",)) == "S003"
    assert new_rule(db, exam.id, UUIDS[4], DISPLAY[4], ("submitted",)) is None
    assert new_rule(db, exam.id, UUIDS[4], DISPLAY[4], ("in_progress",)) == "S004"


@pytest.mark.parametrize("student_id, identifier, expected", [
    ("AAAAAAAA-0000-4000-8000-000000000005", None, "S005"),          # case of the canonical id
    ("  aaaaaaaa-0000-4000-8000-000000000005 ", None, "S005"),        # spaces
    (None, "te-005", "S005"),                                        # the display id in another case
    ("someone-else", " TE-005 ", "S005"),                            # the display id matches, the id does not
    ("aaaaaaaa-0000-4000-8000-000000000099", "TE-099", None),        # an unknown student
    (None, None, None),                                              # no identity: fails closed
    ("", "  ", None),
    ("Ученик 5", None, None),                                        # a NAME is no identity (non-ASCII key: the whole list is scanned, nothing matches)
])
def test_the_cases_of_the_identity_rule(db, student_id, identifier, expected):
    exam = seed(db)
    assert new_rule(db, exam.id, student_id, identifier, ("submitted", "waiting", "in_progress")) == expected
    assert old_rule(db, exam.id, student_id, identifier, ("submitted", "waiting", "in_progress")) == expected


def test_a_legacy_row_that_kept_the_display_id_in_student_id_still_matches(db):
    exam = seed(db, n=3)
    db.add(ExamSession(id="LEGACY", exam_id=exam.id, student_id="te-777", display_id=None, student_name="Старый", variant_number=1, answers={}, max_score=10,
                       status="in_progress", session_token="tok-legacy", started_at=BASE))
    db.add(ExamSession(id="PADDED", exam_id=exam.id, student_id="  TE-888  ", display_id=None, student_name="Пробелы", variant_number=1, answers={}, max_score=10,
                       status="in_progress", session_token="tok-padded", started_at=BASE))
    db.commit()
    for student_id, identifier, expected in [("uuid-new", "TE-777", "LEGACY"), (None, "TE-888", "PADDED")]:
        assert new_rule(db, exam.id, student_id, identifier, ("in_progress",)) == expected == old_rule(db, exam.id, student_id, identifier, ("in_progress",))


def test_a_namesake_never_matches_by_name(db):
    exam = seed(db, n=3)
    db.add(ExamSession(id="NAMESAKE", exam_id=exam.id, student_id="bbbbbbbb-0000-4000-8000-000000000001", display_id="TE-900", student_name="Ученик 1",
                       variant_number=1, answers={}, max_score=10, status="submitted", session_token="tok-n", started_at=BASE))
    db.commit()
    assert new_rule(db, exam.id, UUIDS[1], DISPLAY[1], ("submitted",)) is None
    assert new_rule(db, exam.id, "bbbbbbbb-0000-4000-8000-000000000001", "TE-900", ("submitted",)) == "NAMESAKE"


def test_the_rows_come_back_in_the_order_of_the_old_list(db):
    exam = seed(db, n=3)
    db.add(ExamSession(id="TWIN", exam_id=exam.id, student_id=UUIDS[1], display_id=None, student_name="Дубль", variant_number=1, answers={}, max_score=10,
                       status="in_progress", session_token="tok-twin", started_at=BASE + timedelta(hours=1)))
    db.commit()
    narrow = ids_of(router_module._student_sessions(db, exam.id, UUIDS[1], DISPLAY[1]))
    wide = [s.id for s in SessionRepository(db).list_by_exam(exam.id) if s.id in narrow]
    assert narrow == wide == ["TWIN", "S001"]


def _spy_rows(monkeypatch):
    seen = []
    real = router_module._find_student_session

    def spy(sessions, statuses, student_id, identifier=None):
        seen.append(len(sessions))
        return real(sessions, statuses, student_id, identifier)

    monkeypatch.setattr(router_module, "_find_student_session", spy)
    return seen


def test_check_student_start_and_resume_read_one_students_rows(client, db, monkeypatch):
    """The three entry paths for the same student, new vs old query: same answer; rows read per call 30 -> 1."""
    exam = seed(db)
    seen = _spy_rows(monkeypatch)
    student = {"id": UUIDS[3], "display_id": DISPLAY[3], "full_name": "Ученик 3"}      # submitted
    student7 = {"id": UUIDS[7], "display_id": DISPLAY[7], "full_name": "Ученик 7"}     # in progress (the second half of the class)

    def run():
        out = {}
        out["check_sub"] = router_module._check_student_outcome(db, exam, student).model_dump()
        out["check_open"] = router_module._check_student_outcome(db, exam, student7).model_dump()
        out["resume_open"] = router_module._has_resumable_session(db, exam.id, student7)
        out["resume_sub"] = router_module._has_resumable_session(db, exam.id, student)
        return out

    db.commit()
    seen.clear()
    with count_statements() as new:
        after = run()
    new_rows = list(seen)
    monkeypatch.setattr(router_module, "_student_sessions", lambda db_, exam_id, sid, ident=None: SessionRepository(db_).list_by_exam(exam_id))
    seen.clear()
    with count_statements() as old:
        before = run()
    old_rows = list(seen)

    assert after == before
    assert after["check_sub"]["already_submitted"] is True and after["check_sub"]["session_id"] == "S003"
    assert after["check_open"]["enrolled"] is True and after["resume_open"] is True and after["resume_sub"] is False
    print(f"\nEX-46 4 entry checks: SQL {old.count} -> {new.count}; rows given to the rule {old_rows} -> {new_rows}")
    assert old_rows == [30] * 4 and new_rows == [1] * 4
    assert new.count == old.count


def test_start_of_an_existing_and_of_a_submitted_student_over_http_is_the_same(client, db, monkeypatch):
    """POST /start on the real route (roster stubbed by the conftest): a student who is in answers with HIS session and token, a submitted one is refused a second attempt."""
    exam = seed(db)
    db.commit()
    body7 = {"student_name": "Ученик 7", "student_id": UUIDS[7], "display_id": DISPLAY[7]}
    body3 = {"student_name": "Ученик 3", "student_id": UUIDS[3], "display_id": DISPLAY[3]}
    assert exam.status == "lobby"

    def go():
        a = client.post(f"{API}/exams/by-code/EX46CODE/start", json=body7)
        b = client.post(f"{API}/exams/by-code/EX46CODE/start", json=body3)
        assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
        return a.json(), b.json()

    a_new, b_new = go()
    monkeypatch.setattr(router_module, "_student_sessions", lambda db_, exam_id, sid, ident=None: SessionRepository(db_).list_by_exam(exam_id))
    a_old, b_old = go()
    assert a_new == a_old and b_new == b_old
    assert a_new["session_id"] == "S007" and a_new["session_token"] == "tok7" and a_new["status"] == "waiting"      # the exam is a lobby
    assert b_new["already_submitted"] is True and b_new["session_id"] == "S003"


def test_a_new_student_gets_exactly_one_attempt_and_the_second_start_returns_it(client, db):
    exam = seed(db, n=5)
    body = {"student_name": "Новый Ученик", "student_id": "cccccccc-0000-4000-8000-000000000001", "display_id": "TE-123"}
    first = client.post(f"{API}/exams/by-code/EX46CODE/start", json=body).json()
    second = client.post(f"{API}/exams/by-code/EX46CODE/start", json=body).json()
    assert first["status"] == "waiting" and second["session_id"] == first["session_id"] and second["session_token"] == first["session_token"]
    assert db.query(ExamSession).filter(ExamSession.exam_id == exam.id, ExamSession.student_id == body["student_id"]).count() == 1
