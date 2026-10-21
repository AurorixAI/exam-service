"""LOGIC-B (auth package B) on the exam-service side: O8 (ending a lesson with a live exam), O4 (archiving a group) and O17 (a running exam stays reachable
for a student who was moved).  Markers [O8.R4] [O8.R6] [O17.R1] ... [O17.R9] follow the matrices of artifacts/partner_doc_check/logicB_spec.md."""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

import src.api.router as router
from src.core.config import get_settings
from src.models.exam_models import ExamLifecycleEvent, ExamSession
from tests.conftest import (
    TEST_GROUP_ID, TEST_LESSON_ID, TEST_ORG_ID, make_exam_in_db, make_session_in_db, make_task,
)

REAL_CANONICAL = router._canonical_student_for_exam      # the conftest stub replaces it at fixture time
TOKEN = "logicb-internal-token"
TODAY = date.today().isoformat()
H = {"X-Internal-Service-Token": TOKEN}
LESSON = {"org_id": TEST_ORG_ID, "group_id": TEST_GROUP_ID, "lesson_id": TEST_LESSON_ID, "lesson_date": TODAY}
GROUP = {"org_id": TEST_ORG_ID, "group_id": TEST_GROUP_ID}
U1 = "aaaaaaaa-0000-4000-8000-000000000001"
NOT_FOUND = "Ученик не найден в этой группе — проверьте имя или ID"


@pytest.fixture(autouse=True)
def _internal_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "internal_service_token", TOKEN)


def started(minutes_ago: int) -> dict:
    return {"time_minutes": 45, "mc_mode": "auto", "started_at": (datetime.utcnow() - timedelta(minutes=minutes_ago)).isoformat()}


def waiting_session(db, exam, sid="SES_W1", student=None):
    s = ExamSession(id=sid, exam_id=exam.id, student_id=student, student_name="Ожидающий", variant_number=1, answers={}, max_score=10,
                    status="waiting", session_token=f"tok-{sid}")
    db.add(s)
    db.commit()
    return s


# ======================================================================================= completion-check / completed
def test_completion_check_reports_a_running_exam_with_the_remaining_time_and_a_lobby_with_its_waiting(client, db):
    """The teacher is warned about what is live: an active exam (remaining seconds, ends at, UTC) and a lobby exam (waiting count)."""
    exam = make_exam_in_db(db, status="active")
    exam.settings = started(30)
    db.commit()
    body = client.post("/api/v1/internal/lesson-bindings/completion-check", json=LESSON, headers=H).json()
    item = body["exams"][0]
    assert body["ok"] is True and item["status"] == "active" and item["examId"] == exam.id and item["deliveryMode"] == "online"
    assert 890 <= item["remainingSeconds"] <= 900 and item["endsAt"] and item["waitingCount"] == 0
    exam.status, exam.settings = "lobby", {"time_minutes": 45}
    db.commit()
    waiting_session(db, exam)
    lobby = client.post("/api/v1/internal/lesson-bindings/completion-check", json=LESSON, headers=H).json()["exams"][0]
    assert lobby["status"] == "lobby" and lobby["waitingCount"] == 1 and lobby["remainingSeconds"] is None
    assert client.post("/api/v1/internal/lesson-bindings/completion-check", json=LESSON).status_code in (403, 422)


def test_a_finished_timer_is_not_live(client, db):
    """[O8.R1] 17:20: the exam of 45 minutes that began at 16:30 is over - no warning (the first request after the deadline closes it)."""
    exam = make_exam_in_db(db, status="active")
    exam.settings = started(50)
    db.commit()
    assert client.post("/api/v1/internal/lesson-bindings/completion-check", json=LESSON, headers=H).json()["exams"] == []


def test_completed_leaves_an_active_exam_and_frees_an_untouched_lobby(client, db):
    """[O8.R4] ``completed`` is decided on the state at that instant under the exam lock: an exam that is active is left running (``leftRunning``), a lobby without started attempts goes back
    to a draft - its waiting sessions are deleted, the event ``lesson_completed`` is written; a repeat changes nothing."""
    lobby = make_exam_in_db(db, status="lobby")
    waiting_session(db, lobby)
    r = client.post("/api/v1/internal/lesson-bindings/completed", json={**LESSON, "actor_id": "USR_T"}, headers=H).json()
    assert r == {"ok": True, "released": [lobby.id], "leftRunning": []}
    db.refresh(lobby)
    assert lobby.status in ("variants_ready", "draft") and lobby.access_code is None and lobby.scheduled_lesson_id is None
    assert db.query(ExamSession).filter_by(exam_id=lobby.id).count() == 0
    event = db.query(ExamLifecycleEvent).filter_by(exam_id=lobby.id).order_by(ExamLifecycleEvent.occurred_at.desc()).first()
    assert event.event_type == "lesson_completed" and event.lesson_date == TODAY and event.actor_id == "USR_T"
    assert client.post("/api/v1/internal/lesson-bindings/completed", json=LESSON, headers=H).json() == {"ok": True, "released": [], "leftRunning": []}
    # the same instant, the student has already started: the exam is active -> left running
    lobby.status, lobby.scheduled_lesson_id, lobby.scheduled_lesson_date, lobby.group_id = "active", TEST_LESSON_ID, TODAY, TEST_GROUP_ID
    lobby.settings = started(10)
    db.commit()
    live = client.post("/api/v1/internal/lesson-bindings/completed", json=LESSON, headers=H).json()
    assert live == {"ok": True, "released": [], "leftRunning": [lobby.id]}
    db.refresh(lobby)
    assert lobby.status == "active"


def test_the_remaining_time_is_counted_in_utc_across_the_hour_border(client, db):
    """[O8.R6] An exam that began at 22:50 UTC and is checked at 23:20 UTC has 15 minutes left whatever the local hour: only ``started_at`` (UTC) and the clock matter."""
    exam = make_exam_in_db(db, status="active")
    exam.settings = {"time_minutes": 45, "started_at": (datetime.utcnow() - timedelta(minutes=30)).isoformat()}
    db.commit()
    item = client.post("/api/v1/internal/lesson-bindings/completion-check", json=LESSON, headers=H).json()["exams"][0]
    assert 880 <= item["remainingSeconds"] <= 900


def test_a_freed_exam_can_be_assigned_to_another_lesson_and_a_lobby_with_started_attempts_is_not_freed(client, db):
    """[O8.R3] A released exam keeps its tasks and can be assigned again (its binding is cleared); an exam with a started attempt is never released."""
    exam = make_exam_in_db(db, status="lobby")
    make_session_in_db(db, exam, status="in_progress")
    r = client.post("/api/v1/internal/lesson-bindings/completed", json=LESSON, headers=H).json()
    assert r["released"] == [] and r["leftRunning"] == [exam.id]


# ============================================================================================ group archive
def test_archive_impact_and_release_are_all_or_nothing(client, db):
    """[O4.R1] The impact lists the exams of the group with canRelease; the release frees every untouched one and refuses the whole call (409) while one is running."""
    lobby = make_exam_in_db(db, status="lobby")
    waiting_session(db, lobby)
    impact = client.post("/api/v1/internal/group-bindings/archive-impact", json=GROUP, headers=H).json()
    assert impact == {"exams": [{"examId": lobby.id, "title": lobby.title, "status": "lobby", "lessonDate": TODAY, "canRelease": True, "blockerReason": None}]}
    lobby.status, lobby.settings = "active", started(5)
    db.commit()
    impact = client.post("/api/v1/internal/group-bindings/archive-impact", json=GROUP, headers=H).json()["exams"][0]
    assert impact["canRelease"] is False and impact["blockerReason"] == "Экзамен уже идёт"
    refused = client.post("/api/v1/internal/group-bindings/archive-release", json=GROUP, headers=H)
    assert refused.status_code == 409
    db.refresh(lobby)
    assert lobby.status == "active"
    lobby.status = "lobby"
    lobby.settings = {"time_minutes": 45}
    db.commit()
    ok = client.post("/api/v1/internal/group-bindings/archive-release", json={**GROUP, "actor_id": "USR_T", "reason": "Группа закрыта"}, headers=H).json()
    assert ok == {"ok": True, "releasedExamIds": [lobby.id]}
    db.refresh(lobby)
    assert lobby.status in ("variants_ready", "draft") and lobby.scheduled_lesson_id is None
    events = db.query(ExamLifecycleEvent).filter_by(exam_id=lobby.id).all()
    assert "group_archived" in {e.event_type for e in events}


# ======================================================================================================= O17
class FakeAuth:
    """auth-service ``verify-student``: enrolled only for the requests that carry ``as_of`` (the student was moved after the start)."""

    def __init__(self, enrolled_now: bool = False, enrolled_then: bool = True):
        self.enrolled_now, self.enrolled_then, self.calls = enrolled_now, enrolled_then, []

    def client(self):
        outer = self

        class Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None, headers=None):
                outer.calls.append(dict(params or {}))
                enrolled = outer.enrolled_then if "as_of" in (params or {}) else outer.enrolled_now
                student = {"id": U1, "full_name": "Иванова Мария", "display_id": "TE-040", "class_level": 8, "first_name": "Мария", "last_name": "Иванова"}
                data = {"enrolled": True, "student": student} if enrolled else {"enrolled": False, "message": "нет"}
                return SimpleNamespace(status_code=200, json=lambda: data)

        return Client


def entry(client, db, monkeypatch, *, status="active", settings=None, session=True, auth=None, code="LBCODE01"):
    monkeypatch.setattr(router, "_canonical_student_for_exam", REAL_CANONICAL)
    auth = auth or FakeAuth()
    monkeypatch.setattr(router.httpx, "AsyncClient", auth.client())
    exam = make_exam_in_db(db, status=status)
    exam.access_code = code
    exam.settings = settings if settings is not None else started(15)
    db.commit()
    sess = None
    if session:
        sess = make_session_in_db(db, exam, status="in_progress")
        sess.student_id, sess.display_id, sess.session_token = U1, "TE-040", "capability-1"
        db.commit()
    return exam, sess, auth


START = {"student_name": "Иванова Мария", "student_id": U1, "display_id": "TE-040"}


def test_o17_r1_a_moved_student_resumes_his_attempt_on_a_reload(client, db, monkeypatch):
    """[O17.R1] [O17.R2] [O17.R4] The ordinary check says "not in the group"; the repeat as of the exam's start finds him: the SAME session comes back (same token, saved answers), a second reload too."""
    exam, sess, auth = entry(client, db, monkeypatch)
    sess.answers = {"TASK_001": "3"}
    db.commit()
    first = client.post(f"/api/v1/exams/by-code/{exam.access_code}/start", json=START)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["session_id"] == sess.id and body["status"] == "in_progress" and body["saved_answers"] == {"TASK_001": "3"}
    assert body["session_token"] == "capability-1" and body["tasks"] and 0 < body["time_remaining_seconds"] <= 30 * 60
    second = client.post(f"/api/v1/exams/by-code/{exam.access_code}/start", json=START).json()
    assert second["session_id"] == sess.id and second["session_token"] == "capability-1"
    assert db.query(ExamSession).filter_by(exam_id=exam.id).count() == 1
    assert [("as_of" in c) for c in auth.calls[:2]] == [False, True]                    # first the ordinary check, then the repeat as of the start
    check = client.post(f"/api/v1/exams/by-code/{exam.access_code}/check-student", json={"student_id": U1, "student_name": "Иванова Мария"}).json()
    assert check["enrolled"] is True


def test_o17_r5_r7_without_a_session_he_is_refused_as_before(client, db, monkeypatch):
    """[O17.R5] [O17.R7] Moved BEFORE the start (the repeat does not include him) or no attempt of his own: 403 and no new session."""
    exam, _s, auth = entry(client, db, monkeypatch, session=False)
    r = client.post(f"/api/v1/exams/by-code/{exam.access_code}/start", json=START)
    assert r.status_code == 403 and r.json()["detail"] == NOT_FOUND
    assert db.query(ExamSession).filter_by(exam_id=exam.id).count() == 0
    chk = client.post(f"/api/v1/exams/by-code/{exam.access_code}/check-student", json={"student_id": U1, "student_name": "Иванова Мария"}).json()
    assert chk["enrolled"] is False


def test_o17_r5_moved_before_the_start_is_refused_even_with_a_session(client, db, monkeypatch):
    """[O17.R5] Auth answers "not enrolled" to the repeat as of the start too (he left BEFORE the exam began): refused, the session is not resumed."""
    exam, _s, auth = entry(client, db, monkeypatch, auth=FakeAuth(enrolled_then=False))
    r2 = client.post(f"/api/v1/exams/by-code/{exam.access_code}/start", json=START)
    assert r2.status_code == 403 and any("as_of" in c for c in auth.calls)


def test_o17_r6_a_lobby_student_is_not_resumed(client, db, monkeypatch):
    """[O17.R6] An exam that has not started (lobby, no started_at): the fallback does not apply; ``left_at == started_at`` is refused by auth itself (left_at > instant is false) and arrives as "not enrolled"."""
    exam, _s, auth = entry(client, db, monkeypatch, status="lobby", settings={"time_minutes": 45}, session=False)
    r = client.post(f"/api/v1/exams/by-code/{exam.access_code}/start", json=START)
    assert r.status_code == 403
    assert not any("as_of" in c for c in auth.calls)                                    # no started_at: no repeat at all


def test_o17_r3_r8_r9_a_student_who_is_back_goes_the_ordinary_way_only_students_use_it(client, db, monkeypatch):
    """[O17.R3] [O17.R8] [O17.R9] Back in the group: the ordinary check passes (no repeat); an archived-during-the-exam student is "enrolled as of the start" on the auth side and resumes the same way."""
    exam, sess, auth = entry(client, db, monkeypatch, auth=FakeAuth(enrolled_now=True))
    r = client.post(f"/api/v1/exams/by-code/{exam.access_code}/start", json=START)
    assert r.status_code == 200 and r.json()["session_id"] == sess.id
    assert not any("as_of" in c for c in auth.calls)


def test_o17_r9_archived_during_the_exam_resumes(client, db, monkeypatch):
    """[O17.R9] Archived during the exam: auth says "enrolled as of the start" (archived_at > as_of) - the attempt is resumed."""
    exam, sess, auth = entry(client, db, monkeypatch, auth=FakeAuth(enrolled_now=False, enrolled_then=True))
    again = client.post(f"/api/v1/exams/by-code/{exam.access_code}/start", json=START)
    assert again.status_code == 200 and again.json()["session_id"] == sess.id


def test_session_token_endpoints_do_not_check_the_roster(client, db, monkeypatch):
    """[O17.R1] The endpoints that hold the attempt's capability (status, saving, submitting, the result) never ask auth-service: a student who was moved finishes his exam."""
    exam, sess, _a = entry(client, db, monkeypatch)

    async def boom(*a, **kw):
        raise AssertionError("the roster must not be asked here")

    monkeypatch.setattr(router, "_canonical_student_for_exam", boom)
    r = client.get(f"/api/v1/exams/by-code/{exam.access_code}/session-status/{sess.id}", headers={"X-Exam-Session-Token": "capability-1"})
    assert r.status_code == 200 and r.json()["status"] != "kicked"
