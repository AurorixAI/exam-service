"""#88: an exam is never bound to a lesson that is completed, cancelled or over.

The frontend dialog already hides such lessons (algo-front ``src/lib/exam-lessons.ts`` ``isLessonBindable``); the
server enforces the same rule, so a stale tab or a direct API call cannot bind an exam to a lesson that can never
open it.  Business time is Asia/Tashkent.  A lesson in progress stays bindable even past its planned end.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("APP_ENV", "testing")

from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import HTTPException

from src.api import router
from src.api.router import LESSON_OVER_MESSAGE, _clock_minutes, _lesson_is_bindable
from src.core.auth import ExamActor
from tests.conftest import TEST_GROUP, TEST_GROUP_ID, TEST_LESSON_ID, make_exam_in_db, make_task

REAL_RESOLVE = router._resolve_lesson_occurrence  # the ``client`` fixtures replace it on the module

TODAY = date(2026, 10, 16)
TODAY_S = TODAY.isoformat()
TOMORROW_S = "2026-10-17"
YESTERDAY_S = "2026-10-15"
MESSAGE = "Занятие уже прошло или отменено — выберите другое"


def bindable(lesson_date, end="11:30", status=None, now="10:00", today=TODAY):
    hours, minutes = now.split(":")
    return _lesson_is_bindable(
        lesson_date=lesson_date,
        end_time=end,
        lesson_status=status,
        today=today,
        now_minutes=int(hours) * 60 + int(minutes),
    )


class TestTheRule:
    """Case by case the table of ``isLessonBindable``."""

    def test_message_is_the_agreed_one(self):
        assert LESSON_OVER_MESSAGE == MESSAGE

    @pytest.mark.parametrize("status", ["completed", "cancelled"])
    @pytest.mark.parametrize("day", [YESTERDAY_S, TODAY_S, TOMORROW_S])
    def test_completed_or_cancelled_is_never_bindable(self, status, day):
        assert bindable(day, status=status) is False

    def test_an_earlier_day_is_not_bindable(self):
        assert bindable(YESTERDAY_S) is False
        assert bindable(YESTERDAY_S, status="in_progress") is False

    def test_a_later_day_is_bindable(self):
        assert bindable(TOMORROW_S) is True
        assert bindable("2027-01-01", end="08:00") is True

    def test_today_before_the_planned_end_is_bindable(self):
        assert bindable(TODAY_S, end="11:30", now="10:00") is True
        assert bindable(TODAY_S, end="11:30", now="11:29") is True

    def test_today_from_the_planned_end_on_is_over(self):
        assert bindable(TODAY_S, end="11:30", now="11:30") is False
        assert bindable(TODAY_S, end="11:30", now="15:00") is False

    def test_in_progress_past_its_planned_end_is_still_bindable(self):
        assert bindable(TODAY_S, end="11:30", now="12:45", status="in_progress") is True

    def test_today_without_an_end_time_is_not_judged_over(self):
        assert bindable(TODAY_S, end=None, now="23:00") is True
        assert bindable(TODAY_S, end="", now="23:00") is True
        assert bindable(TODAY_S, end="   ", now="23:00") is True

    def test_a_malformed_end_time_is_over_like_in_the_dialog(self):
        """The frontend reads «soon» as 0 minutes (``Number("soon") || 0``): the lesson is over and the dialog hides it."""
        assert bindable(TODAY_S, end="soon", now="00:01") is False
        assert bindable(TODAY_S, end="soon", now="23:00") is False
        assert bindable(TODAY_S, end="soon", now="23:00", status="in_progress") is True  # in progress stays bindable
        assert bindable(TOMORROW_S, end="soon", now="23:00") is True

    def test_clock_minutes_reads_the_way_the_frontend_does(self):
        assert _clock_minutes("11:30") == 690
        assert _clock_minutes("09:05:00") == 545
        assert _clock_minutes("00:00") == 0
        assert _clock_minutes("11") == 660  # a missing minutes part is 0
        assert _clock_minutes("25:99") == 25 * 60 + 99  # no range check, as in the dialog
        assert _clock_minutes("soon") == 0 and _clock_minutes("x:y") == 0 and _clock_minutes(":") == 0
        assert _clock_minutes(None) is None and _clock_minutes("") is None


# ── resolve against a mocked auth-service ───────────────────────────────────────────────────────────────────

class _Auth:
    """What auth-service answers for the schedule and the attendance of one group/day."""

    def __init__(self, *, end="11:30", status=None, attendance_http=200, attendance_error=False, listed=True):
        self.end, self.status = end, status
        self.attendance_http, self.attendance_error, self.listed = attendance_http, attendance_error, listed
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        date_from = request.url.params.get("date_from")
        if request.url.path.endswith("/schedule/occurrences"):
            items = [{
                "id": TEST_LESSON_ID, "groupId": TEST_GROUP_ID, "occurrenceDate": date_from,
                "startTime": "10:00", "endTime": self.end,
            }] if self.listed else []
            return httpx.Response(200, json=items)
        if request.url.path.endswith("/attendance"):
            if self.attendance_error:
                raise httpx.ConnectError("auth-service is down")
            statuses = {f"{date_from}_{TEST_LESSON_ID}": self.status} if self.status else {}
            return httpx.Response(self.attendance_http, json={"statuses": statuses})
        return httpx.Response(404)


@pytest.fixture
def auth(monkeypatch):
    """Route router's outbound httpx calls to a scripted auth-service and pin the business clock."""

    def install(spec: _Auth, *, now="10:00"):
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(spec.handler)
            return real(*args, **kwargs)

        async def group_access(actor, group_id):
            return dict(TEST_GROUP)

        monkeypatch.setattr(router.httpx, "AsyncClient", factory)
        monkeypatch.setattr(router, "require_group_access", group_access)
        hours, minutes = now.split(":")
        instant = datetime(TODAY.year, TODAY.month, TODAY.day, int(hours), int(minutes), tzinfo=ZoneInfo("Asia/Tashkent"))
        monkeypatch.setattr(router, "_business_now", lambda: instant)
        monkeypatch.setattr(router, "_business_today", lambda: TODAY)
        return spec

    return install


def _actor():
    return ExamActor(user_id="teacher-1", org_id="org-1", role="teacher", auth_headers={"authorization": "Bearer t"})


def resolve(lesson_date, **kwargs):
    return asyncio.run(
        REAL_RESOLVE(_actor(), group_id=TEST_GROUP_ID, lesson_id=TEST_LESSON_ID, lesson_date=lesson_date, **kwargs)
    )


def refused(lesson_date, **kwargs) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        resolve(lesson_date, **kwargs)
    return caught.value


class TestResolveEnforcesTheRule:
    def test_a_future_lesson_is_allowed(self, auth):
        auth(_Auth())
        group, occurrence = resolve(TOMORROW_S)
        assert occurrence["occurrenceDate"] == TOMORROW_S and group["id"] == TEST_GROUP_ID

    def test_today_before_its_end_is_allowed(self, auth):
        auth(_Auth(end="11:30"), now="10:15")
        assert resolve(TODAY_S)[1]["id"] == TEST_LESSON_ID

    def test_today_in_progress_past_its_end_is_allowed(self, auth):
        auth(_Auth(end="11:30", status="in_progress"), now="12:40")
        assert resolve(TODAY_S)[1]["id"] == TEST_LESSON_ID

    def test_today_past_its_end_and_never_started_is_refused(self, auth):
        auth(_Auth(end="11:30"), now="15:00")
        error = refused(TODAY_S)
        assert (error.status_code, error.detail) == (409, MESSAGE)

    @pytest.mark.parametrize("status", ["completed", "cancelled"])
    def test_a_completed_or_cancelled_lesson_is_refused_even_before_its_end(self, auth, status):
        auth(_Auth(end="23:00", status=status), now="10:00")
        error = refused(TODAY_S)
        assert (error.status_code, error.detail) == (409, MESSAGE)

    def test_a_cancelled_lesson_of_a_later_day_is_refused(self, auth):
        auth(_Auth(status="cancelled"))
        error = refused(TOMORROW_S)
        assert (error.status_code, error.detail) == (409, MESSAGE)

    def test_a_past_day_is_refused_without_asking_auth_service(self, auth):
        spec = auth(_Auth())
        error = refused(YESTERDAY_S)
        assert (error.status_code, error.detail) == (409, MESSAGE)
        assert spec.requests == []

    def test_a_lesson_that_is_not_in_the_schedule_is_still_a_400(self, auth):
        auth(_Auth(listed=False))
        assert refused(TOMORROW_S).status_code == 400

    def test_attendance_unavailable_fails_closed(self, auth):
        auth(_Auth(attendance_http=500))
        assert refused(TOMORROW_S).status_code == 503

    def test_attendance_network_error_fails_closed(self, auth):
        auth(_Auth(attendance_error=True))
        assert refused(TOMORROW_S).status_code == 503

    def test_the_starting_path_is_not_subject_to_the_rule(self, auth):
        """_require_lesson_in_progress resolves with require_current_or_future=False: a lesson in progress that has run
        past its end must still resolve there (and no attendance call is made by the resolver)."""
        spec = auth(_Auth(end="11:30", status="in_progress"), now="13:00")
        group, occurrence = resolve(TODAY_S, require_current_or_future=False)
        assert occurrence["id"] == TEST_LESSON_ID
        assert not [path for path in spec.requests if path.endswith("/attendance")]


# ── the endpoints that bind an exam to a lesson ─────────────────────────────────────────────────────────────

TASKS = [make_task("TASK_001")]


@pytest.fixture
def draft(db):
    exam = make_exam_in_db(db, status="draft", tasks=TASKS)
    exam.scheduled_lesson_id = None
    exam.scheduled_lesson_date = None
    exam.group_id = None
    exam.access_code = None
    db.commit()
    return exam


@pytest.fixture
def real_resolver(monkeypatch):
    monkeypatch.setattr(router, "_resolve_lesson_occurrence", REAL_RESOLVE)


def _bind_requests(day):
    body = {"lesson_id": TEST_LESSON_ID, "lesson_date": day}
    return {
        "publish": ("/api/v1/exams/EXAM_TEST01/publish", {**body, "group_id": TEST_GROUP_ID}),
        "assign": ("/api/v1/exams/EXAM_TEST01/assign", {**body, "group_id": TEST_GROUP_ID, "publish": True}),
        "open-lobby": ("/api/v1/exams/EXAM_TEST01/open-lobby", body),
    }


@pytest.mark.parametrize("endpoint", ["publish", "assign", "open-lobby"])
class TestEndpointsRefuseAnOverLesson:
    def _post(self, client, endpoint, day):
        path, body = _bind_requests(day)[endpoint]
        return client.post(path, json=body)

    def _prepare(self, db, draft, endpoint):
        if endpoint == "open-lobby":
            draft.group_id = TEST_GROUP_ID  # open-lobby re-binds an exam that already has a group
            db.commit()

    def test_over_today_is_409_with_the_message_and_nothing_is_written(self, client, db, draft, real_resolver, auth, endpoint):
        auth(_Auth(end="11:30"), now="15:00")
        self._prepare(db, draft, endpoint)
        response = self._post(client, endpoint, TODAY_S)
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == MESSAGE
        db.refresh(draft)
        assert draft.status == "draft" and draft.access_code is None and draft.scheduled_lesson_id is None

    def test_completed_is_409(self, client, db, draft, real_resolver, auth, endpoint):
        auth(_Auth(end="23:00", status="completed"), now="10:00")
        self._prepare(db, draft, endpoint)
        response = self._post(client, endpoint, TODAY_S)
        assert response.status_code == 409 and response.json()["detail"] == MESSAGE

    def test_cancelled_is_409(self, client, db, draft, real_resolver, auth, endpoint):
        auth(_Auth(status="cancelled"))
        self._prepare(db, draft, endpoint)
        response = self._post(client, endpoint, TOMORROW_S)
        assert response.status_code == 409 and response.json()["detail"] == MESSAGE

    def test_in_progress_past_its_end_is_allowed(self, client, db, draft, real_resolver, auth, endpoint):
        auth(_Auth(end="11:30", status="in_progress"), now="12:45")
        self._prepare(db, draft, endpoint)
        response = self._post(client, endpoint, TODAY_S)
        assert response.status_code == 200, response.text
        db.refresh(draft)
        assert draft.status == "lobby" and draft.scheduled_lesson_date == TODAY_S

    def test_a_future_lesson_is_allowed(self, client, db, draft, real_resolver, auth, endpoint):
        auth(_Auth())
        self._prepare(db, draft, endpoint)
        response = self._post(client, endpoint, TOMORROW_S)
        assert response.status_code == 200, response.text
        db.refresh(draft)
        assert draft.status == "lobby" and draft.scheduled_lesson_date == TOMORROW_S


def test_the_business_clock_is_read_in_tashkent_once(monkeypatch):
    seen = []

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            seen.append(str(tz))
            return datetime(2026, 10, 16, 13, 45, tzinfo=tz)

    monkeypatch.setattr(router, "datetime", Fixed)
    instant = router._business_now()
    assert (instant.date(), instant.hour * 60 + instant.minute) == (date(2026, 10, 16), 13 * 60 + 45)
    assert router._business_today() == date(2026, 10, 16)
    assert seen == ["Asia/Tashkent", "Asia/Tashkent"]


def test_the_binding_check_reads_the_clock_once_for_the_date_and_the_minutes(auth, monkeypatch):
    """Midnight race: a date taken from one reading and minutes from another could put 23:59 on the next day."""
    spec = auth(_Auth(end="23:59"), now="23:58")
    readings = []
    real_now = router._business_now

    def counting():
        readings.append(1)
        return real_now()

    monkeypatch.setattr(router, "_business_now", counting)
    resolve(TODAY_S)
    assert len(readings) == 1
    assert spec.requests
