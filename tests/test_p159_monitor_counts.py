"""#159 E1: ``GET /admin/exams/monitor`` counts the lobbies by the day of their lesson (``counts.lobby_future`` / ``lobby_today`` / ``lobby_past``) and filters by ``state=lobby_future|lobby_today``.

The day is the BUSINESS day (Asia/Tashkent) taken from ``_business_today`` - the clock of the test is fixed just after the Tashkent midnight, when UTC is still the day before.
SQL only, no call to auth-service: the web app asks for the status of a lesson only for the lobbies of today."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.api import router as router_module
from src.models.exam_models import GeneratedExam
from tests.conftest import TEST_GROUP_ID, TEST_LESSON_ID, TEST_ORG_ID, TEST_USER_ID, make_actor, make_task

API = "/api/v1"
TASHKENT = ZoneInfo("Asia/Tashkent")


@pytest.fixture
def admin_client(client, monkeypatch):
    from src.main import app
    from src.core.auth import require_exam_staff

    app.dependency_overrides[require_exam_staff] = lambda: make_actor("admin")
    # 00:30 of 29.10 in Tashkent = 19:30 of 28.10 UTC: a count by the UTC day would be a day off
    monkeypatch.setattr(router_module, "_business_now", lambda: datetime(2026, 10, 29, 0, 30, tzinfo=TASHKENT))
    return client


def exam(db, exam_id, status, day, org=TEST_ORG_ID):
    db.add(GeneratedExam(
        id=exam_id, title=exam_id, source_mode="topics", variants=[{"variant_number": 1, "tasks": [make_task()]}], settings={"time_minutes": 45, "mc_mode": "auto"},
        skills_covered=["SK_001"], toc_ids_used=[], status=status, total_time_minutes=45, variants_count=1, tasks_per_variant=1, org_id=org, created_by=TEST_USER_ID,
        group_id=TEST_GROUP_ID, group_name="8А", class_level=8, scheduled_lesson_id=TEST_LESSON_ID if day else None, scheduled_lesson_date=day,
        created_at=datetime(2026, 9, 1, 10, 0), updated_at=datetime(2026, 9, 1, 10, 0)))   # created long ago: only the lesson date puts a run into a period
    db.commit()


@pytest.fixture
def world(db):
    exam(db, "F1", "lobby", "2026-10-30")                 # tomorrow
    exam(db, "F2", "lobby", "2026-11-03")                 # in five days
    exam(db, "T1", "lobby", "2026-10-29")                 # today
    exam(db, "P1", "lobby", "2026-10-28")                 # yesterday: a lobby that was never run
    exam(db, "P2", "lobby", None)                         # no lesson at all
    exam(db, "A1", "active", "2026-10-29")
    exam(db, "C1", "closed", "2026-10-28")
    exam(db, "OTHER", "lobby", "2026-10-30", org="ORG_OTHER")   # another centre never counts


def monitor(client, **params):
    r = client.get(f"{API}/admin/exams/monitor", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def ids(body):
    return sorted(i["exam_id"] for i in body["items"])


def test_the_counters_split_the_lobbies_by_the_business_day(admin_client, world):
    counts = monitor(admin_client)["counts"]
    assert (counts["lobby_future"], counts["lobby_today"], counts["lobby_past"]) == (2, 1, 2)
    assert counts["lobby"] == 5 == counts["lobby_future"] + counts["lobby_today"] + counts["lobby_past"]      # disjoint, they cover every lobby
    assert (counts["all"], counts["active"], counts["closed"]) == (7, 1, 1)                                  # the old counters are unchanged


def test_the_filters_return_exactly_what_the_counters_count(admin_client, world):
    future, today = monitor(admin_client, state="lobby_future"), monitor(admin_client, state="lobby_today")
    assert (ids(future), future["total"]) == (["F1", "F2"], 2)
    assert (ids(today), today["total"]) == (["T1"], 1)
    assert ids(monitor(admin_client, state="lobby")) == ["F1", "F2", "P1", "P2", "T1"]                       # the old filter still means every lobby


def test_the_counters_follow_the_period_like_the_other_ones(admin_client, world):
    body = monitor(admin_client, date_from="2026-10-29", date_to="2026-10-29")
    c = body["counts"]
    assert (c["lobby_future"], c["lobby_today"]) == (0, 1)                                                     # a lobby of tomorrow is outside «today»: the web app asks without a period
    assert ids(monitor(admin_client, state="lobby_future", date_from="2026-10-29", date_to="2026-10-29")) == []


def test_a_bad_state_is_still_refused(admin_client, world):
    assert admin_client.get(f"{API}/admin/exams/monitor", params={"state": "lobby_later"}).status_code == 422
