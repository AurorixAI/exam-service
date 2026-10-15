"""N13: an exam attempt belongs to a canonical student (UUID), never to a name.

Two students of one group may share a full name ("Иванова Мария" TE-040 and
TE-041).  The auth-service tells them apart by ID; the session lookup must too,
otherwise the second one is handed the first one's attempt (token, saved answers)
or "you have already finished".
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("APP_ENV", "testing")

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api import router
from src.api.router import _find_active_session, _find_submitted_session
from src.models.exam_models import ExamSession
from tests.conftest import make_exam_in_db, make_session_in_db, make_task

U1 = "aaaaaaaa-0000-4000-8000-000000000001"
U2 = "aaaaaaaa-0000-4000-8000-000000000002"
U3 = "aaaaaaaa-0000-4000-8000-000000000003"
NAME = "Иванова Мария"

ROSTER = [
    {"id": U1, "full_name": NAME, "display_id": "TE-040", "class_level": 8},
    {"id": U2, "full_name": NAME, "display_id": "TE-041", "class_level": 8},
    {"id": U3, "full_name": "Петров Пётр", "display_id": "TE-042", "class_level": 8},
]
TASKS = [make_task(f"TASK_{i:03d}", answer=str(i * 3)) for i in range(1, 4)]


def sess(status="in_progress", student_id=None, display_id=None, name=NAME, sid="S"):
    return SimpleNamespace(
        id=sid, status=status, student_id=student_id, display_id=display_id, student_name=name
    )


# ── pure lookup ──────────────────────────────────────────────────────────────

class TestLookupByIdentity:
    def test_namesake_does_not_get_the_other_active_session(self):
        first = sess(student_id=U1, display_id="TE-040", sid="A")
        assert _find_active_session([first], U2, "TE-041") is None
        assert _find_active_session([first], U1, "TE-040") is first

    def test_namesake_is_not_told_the_other_already_finished(self):
        first = sess("submitted", student_id=U1, display_id="TE-040", sid="A")
        assert _find_submitted_session([first], U2, "TE-041") is None
        assert _find_submitted_session([first], U1, "TE-040") is first

    def test_each_namesake_finds_only_their_own_among_both(self):
        a = sess(student_id=U1, display_id="TE-040", sid="A")
        b = sess(student_id=U2, display_id="TE-041", sid="B")
        assert _find_active_session([a, b], U2, "TE-041") is b
        assert _find_active_session([a, b], U1, "TE-040") is a
        assert _find_active_session([b, a], U1, "TE-040") is a

    def test_status_filters_stay_separate(self):
        done = sess("submitted", student_id=U1, sid="A")
        assert _find_active_session([done], U1, None) is None
        live = sess("waiting", student_id=U1, sid="B")
        assert _find_submitted_session([live], U1, None) is None
        assert _find_active_session([live], U1, None) is live

    def test_uuid_match_is_case_and_space_insensitive(self):
        row = sess(student_id=U1, sid="A")
        assert _find_active_session([row], f" {U1.upper()} ", None) is row

    def test_display_id_equality_matches(self):
        row = sess(student_id=None, display_id="TE-040", sid="A")
        other = sess(student_id=U3, sid="C", name="Петров Пётр")
        assert _find_active_session([row, other], U1, "te-040") is row

    def test_old_rows_that_kept_the_display_id_in_student_id_still_match(self):
        row = sess(student_id="AL-0007", sid="A")
        assert _find_active_session([row], U1, "AL-0007") is row
        assert _find_active_session([row], U2, "AL-0008") is None

    def test_a_different_identity_never_matches_by_name(self):
        row = sess(student_id=U1, display_id="TE-040", sid="A")
        assert _find_active_session([row], U2, None) is None
        assert _find_active_session([row], None, None) is None


class TestNoMatchByNameAlone:
    """A session without identity is never found through a name (fail closed)."""

    def test_nameless_lookup_matches_nothing(self):
        row = sess(student_id=U1, display_id="TE-040")
        assert _find_active_session([row], None, None) is None
        assert _find_submitted_session([sess("submitted", student_id=U1)], "", "") is None

    def test_session_without_identity_is_not_found_by_its_name(self):
        legacy = sess(student_id=None, display_id=None, name="Иван Иванов", sid="L")
        assert _find_active_session([legacy], U3, "TE-050") is None
        done = sess("submitted", student_id=None, display_id=None, name="Иван Иванов", sid="L")
        assert _find_submitted_session([done], U3, "TE-050") is None

    def test_even_when_the_name_is_unique_in_the_exam(self):
        legacy = sess(student_id=None, name="Иван Иванов", sid="L")
        other = sess(student_id=U3, name="Петров Пётр", sid="P")
        assert _find_active_session([legacy, other], U1, "TE-040") is None

    def test_session_tied_to_another_display_id_is_not_open_to_a_namesake(self):
        tied = sess(student_id=None, display_id="TE-040", sid="L")
        assert _find_active_session([tied], U2, "TE-041") is None
        assert _find_active_session([tied], U1, "TE-040") is tied


# ── endpoints ────────────────────────────────────────────────────────────────

@pytest.fixture
def roster(monkeypatch):
    """auth-service stand-in: the group roster above, strict like the real one."""

    async def canonical(exam, *, student_id, student_name, identifier=None):
        key = (student_id or identifier or "").strip().lower()
        if key:
            for st in ROSTER:
                if key in (st["id"].lower(), st["display_id"].lower()):
                    return dict(st)
            raise HTTPException(403, "Ученик не найден в группе")
        matches = [st for st in ROSTER if st["full_name"].lower() == (student_name or "").strip().lower()]
        if len(matches) != 1:
            raise HTTPException(403, "Введите ID ученика")
        return dict(matches[0])

    monkeypatch.setattr(router, "_canonical_student_for_exam", canonical)


def _exam(db, code="EXNAME001"):
    exam = make_exam_in_db(db, status="active", tasks=TASKS)
    exam.access_code = code
    db.commit()
    return exam


def _start(client, code, st):
    resp = client.post(
        f"/api/v1/exams/by-code/{code}/start",
        json={"student_name": NAME, "student_id": st["id"], "display_id": st["display_id"]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _save(client, started, answers):
    resp = client.post(
        f"/api/v1/exam-sessions/{started['session_id']}/progress",
        json={"answers": answers},
        headers={"X-Exam-Session-Token": started["session_token"]},
    )
    assert resp.status_code == 200, resp.text


def _check(client, code, st):
    resp = client.post(
        f"/api/v1/exams/by-code/{code}/check-student",
        json={"student_id": st["id"], "identifier": st["display_id"], "student_name": NAME},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestNamesakesThroughTheApi:
    def test_second_namesake_gets_own_session_and_cannot_touch_the_first(self, client, db, roster):
        _exam(db)
        first = _start(client, "EXNAME001", ROSTER[0])
        _save(client, first, {"TASK_001": "3", "TASK_002": "6"})

        second = _start(client, "EXNAME001", ROSTER[1])
        assert second["session_id"] != first["session_id"]
        assert second["session_token"] != first["session_token"]
        assert second["status"] == "in_progress"
        assert not second.get("already_submitted")
        assert second["saved_answers"] == {}

        _save(client, second, {"TASK_001": "999"})

        rows = {r.student_id: r for r in db.query(ExamSession).all()}
        assert set(rows) == {U1, U2}
        assert rows[U1].answers == {"TASK_001": "3", "TASK_002": "6"}
        assert rows[U2].answers == {"TASK_001": "999"}
        assert rows[U1].display_id == "TE-040" and rows[U2].display_id == "TE-041"

        again = _start(client, "EXNAME001", ROSTER[0])
        assert again["session_id"] == first["session_id"]
        assert again["session_token"] == first["session_token"]
        assert again["saved_answers"] == {"TASK_001": "3", "TASK_002": "6"}

    def test_finishing_one_namesake_does_not_close_the_other(self, client, db, roster):
        _exam(db)
        first = _start(client, "EXNAME001", ROSTER[0])
        sub = client.post(
            f"/api/v1/exam-sessions/{first['session_id']}/submit",
            json={"answers": {"TASK_001": "3"}, "time_spent_seconds": 60},
            headers={"X-Exam-Session-Token": first["session_token"]},
        )
        assert sub.status_code == 200, sub.text

        done = _check(client, "EXNAME001", ROSTER[0])
        assert done["already_submitted"] is True
        assert done["session_id"] == first["session_id"]

        other = _check(client, "EXNAME001", ROSTER[1])
        assert other["enrolled"] is True
        assert other["already_submitted"] is False
        assert other["session_id"] is None

        second = _start(client, "EXNAME001", ROSTER[1])
        assert not second.get("already_submitted")
        assert second["session_id"] != first["session_id"]
        assert second["status"] == "in_progress"

        retake = _start(client, "EXNAME001", ROSTER[0])
        assert retake["already_submitted"] is True
        assert retake["session_id"] == first["session_id"]

    def test_new_session_stores_canonical_uuid_and_display_id(self, client, db, roster):
        _exam(db)
        _start(client, "EXNAME001", ROSTER[2])
        row = db.query(ExamSession).one()
        assert row.student_id == U3
        assert row.display_id == "TE-042"
        assert row.student_name == "Петров Пётр"


class TestSessionWithoutIdentityThroughTheApi:
    def test_a_session_without_student_id_is_not_resumed_by_name(self, client, db, roster, monkeypatch):
        exam = _exam(db, "EXLEGACY01")
        orphan = make_session_in_db(db, exam)  # student_id=None, "Иван Иванов"
        orphan.answers = {"TASK_001": "3"}
        db.commit()

        async def canonical(exam, *, student_id, student_name, identifier=None):
            return {"id": U3, "full_name": "Иван Иванов", "display_id": "TE-050", "class_level": 8}

        monkeypatch.setattr(router, "_canonical_student_for_exam", canonical)

        check = client.post(
            "/api/v1/exams/by-code/EXLEGACY01/check-student",
            json={"student_name": "Иван Иванов"},
        )
        assert check.status_code == 200, check.text
        assert check.json()["already_submitted"] is False

        resp = client.post(
            "/api/v1/exams/by-code/EXLEGACY01/start",
            json={"student_name": "Иван Иванов"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["session_id"] != orphan.id
        assert body["saved_answers"] == {}

        db.refresh(orphan)
        assert orphan.student_id is None
        assert orphan.answers == {"TASK_001": "3"}
        mine = db.query(ExamSession).filter(ExamSession.student_id == U3).one()
        assert mine.id == body["session_id"]
        assert mine.display_id == "TE-050"

    def test_a_submitted_session_without_student_id_does_not_close_the_exam_for_a_namesake(
        self, client, db, roster, monkeypatch
    ):
        exam = _exam(db, "EXLEGACY02")
        done = make_session_in_db(db, exam, status="submitted")  # student_id=None, "Иван Иванов"

        async def canonical(exam, *, student_id, student_name, identifier=None):
            return {"id": U3, "full_name": "Иван Иванов", "display_id": "TE-050", "class_level": 8}

        monkeypatch.setattr(router, "_canonical_student_for_exam", canonical)
        resp = client.post(
            "/api/v1/exams/by-code/EXLEGACY02/start",
            json={"student_name": "Иван Иванов"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert not body.get("already_submitted")
        assert body["session_id"] != done.id
