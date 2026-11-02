"""WC-B EX-07: the lists of exams (the library, the live board, the admin monitor, the sweeper) do not read the task JSON of every exam.

50 exams x 8 variants x 25 tasks (~280 KB of ``variants`` each = ~14 MB) used to travel for ONE open library screen.  The helper
``_without_task_json`` defers the column; the "before" numbers below are the same requests with that helper switched to the identity.
The answer of every list must be identical, the number of statements the same, and the SELECTs that carry ``variants`` zero (a hidden
access in a loop would show up as N extra single-column SELECTs).  The router clock is frozen (LESSONS 76/82)."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import src.main as main_module
from src.api import router as router_module
from src.models.exam_models import ExamSession, GeneratedExam
from tests.conftest import TEST_GROUP_ID, TEST_LESSON_ID, TEST_ORG_ID, TEST_USER_ID, make_actor, make_task
from tests.sql_probe import count_statements

API = "/api/v1"
NOW = datetime(2026, 11, 2, 9, 30, 0)
TODAY = "2026-11-02"


class FrozenDatetime(datetime):
    @classmethod
    def utcnow(cls):
        return cls(NOW.year, NOW.month, NOW.day, NOW.hour, NOW.minute, NOW.second)

    @classmethod
    def now(cls, tz=None):
        return cls(NOW.year, NOW.month, NOW.day, NOW.hour, NOW.minute, NOW.second, tzinfo=tz)


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    monkeypatch.setattr(router_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(router_module, "_business_now", lambda: datetime(2026, 11, 2, 14, 30, tzinfo=ZoneInfo("Asia/Tashkent")))


def big_variants(variants=8, tasks=25):
    out = []
    for v in range(1, variants + 1):
        ts = []
        for i in range(tasks):
            t = make_task(task_id=f"T{v}_{i}", text="Решите уравнение: " + "x + 1 = 2; " * 12, answer="3", answer_type="multiple_choice",
                          answer_options=["1", "2", "3", "4"])
            t["distractor_meta"] = [{"value": "1", "explanation": "Ученик забыл знак. " * 8}]
            ts.append(t)
        out.append({"variant_number": v, "tasks": ts})
    return out


def add_exam(db, i, status, variants, *, started_at=datetime(2026, 11, 2, 9, 20, 0)):
    settings = {"time_minutes": 45, "mc_mode": "auto"}
    if status in ("active", "closed"):
        settings["started_at"] = started_at.isoformat()
    db.add(GeneratedExam(
        id=f"EX{i:03d}", title=f"Экзамен {i}", source_mode="topics", variants=variants, settings=settings,
        skills_covered=["SK_001"], toc_ids_used=[], status=status, total_time_minutes=45, variants_count=len(variants), tasks_per_variant=25,
        org_id=TEST_ORG_ID, created_by=TEST_USER_ID, group_id=TEST_GROUP_ID, group_name="8А", class_level=8,
        scheduled_lesson_id=TEST_LESSON_ID if status != "draft" else None,
        scheduled_lesson_date=TODAY if status != "draft" else None,
        access_code=f"CODE{i:04d}" if status in ("lobby", "active", "closed") else None,
        created_at=datetime(2026, 10, 1, 10, 0, i % 60), updated_at=datetime(2026, 11, 2, 9, 0, i % 60)))


@pytest.fixture
def fifty(db):
    variants = big_variants()
    plan = ["draft"] * 10 + ["lobby"] * 15 + ["active"] * 15 + ["closed"] * 10
    for i, status in enumerate(plan):
        add_exam(db, i, status, variants)
    db.commit()
    for i, status in enumerate(plan):
        if status in ("lobby", "active", "closed"):
            db.add(ExamSession(id=f"S{i:03d}", exam_id=f"EX{i:03d}", student_name="Иван", variant_number=1, answers={}, max_score=250,
                               status="submitted" if i % 2 else "in_progress", session_token=f"tok{i}"))
    db.commit()
    db.expunge_all()
    return len(json.dumps(variants)) * len(plan)


@pytest.fixture
def admin_client(client):
    from src.main import app
    from src.core.auth import require_exam_staff

    app.dependency_overrides[require_exam_staff] = lambda: make_actor("admin")
    return client


def both(monkeypatch, db, call):
    """The answer and the statement counts of ``call`` with the new query and with the old one (no deferral)."""
    db.commit()
    db.expunge_all()
    with count_statements() as new:
        after = call()
    db.commit()
    db.expunge_all()
    monkeypatch.setattr(router_module, "_without_task_json", lambda q: q)
    monkeypatch.setattr(main_module, "_without_task_json", lambda q: q)
    with count_statements() as old:
        before = call()
    return after, before, new, old


def report(name, old, new):
    print(f"\nEX-07 {name}: SQL {old.count} -> {new.count}; SELECT with variants {old.reading('generated_exams', 'variants')} -> "
          f"{new.reading('generated_exams', 'variants')}")


def test_the_library_page_of_50_exams(admin_client, db, fifty, monkeypatch):
    def call():
        r = admin_client.get(f"{API}/exams", params={"page": 1, "size": 50})
        assert r.status_code == 200, r.text
        return r.json()

    after, before, new, old = both(monkeypatch, db, call)
    assert after == before and len(after["items"]) == 50
    report(f"library page of 50 ({fifty // 1_000_000} MB of JSONB in the table)", old, new)
    assert old.reading("generated_exams", "variants") >= 1
    assert new.reading("generated_exams", "variants") == 0
    assert new.count <= old.count


def test_the_live_board(client, db, fifty, monkeypatch):
    def call():
        r = client.get(f"{API}/exams/live-summary")
        assert r.status_code == 200, r.text
        return r.json()

    after, before, new, old = both(monkeypatch, db, call)
    assert after == before and len(after) == 40
    report("live-summary (40 live/recent exams)", old, new)
    assert old.reading("generated_exams", "variants") >= 1
    assert new.reading("generated_exams", "variants") == 0
    assert new.count <= old.count


def test_the_admin_monitor(admin_client, db, fifty, monkeypatch):
    def call():
        r = admin_client.get(f"{API}/admin/exams/monitor", params={"limit": 50})
        assert r.status_code == 200, r.text
        return r.json()

    after, before, new, old = both(monkeypatch, db, call)
    assert after == before and after["total"] == 50
    report("admin monitor (all 50)", old, new)
    assert old.reading("generated_exams", "variants") >= 1
    assert new.reading("generated_exams", "variants") == 0
    assert new.count <= old.count


def test_the_admin_monitor_still_closes_an_expired_active_exam(admin_client, db, monkeypatch):
    """The candidate list of the monitor now holds only the active exams - the one whose timer ran out is still closed and graded."""
    add_exam(db, 1, "active", big_variants(2, 3), started_at=datetime(2026, 11, 2, 8, 0, 0))
    add_exam(db, 2, "lobby", big_variants(2, 3))
    db.commit()
    db.expunge_all()
    body = admin_client.get(f"{API}/admin/exams/monitor").json()
    assert db.get(GeneratedExam, "EX001").status == "closed"
    assert {i["exam_id"]: i["status"] for i in body["items"]} == {"EX001": "closed", "EX002": "lobby"}


def test_the_sweeper_closes_an_expired_exam_and_reads_the_task_json_only_there(db, monkeypatch):
    """Twice a minute on every worker: 15 running exams read no JSONB; the one whose time ran out is closed (the finalize reads what it needs)."""
    variants = big_variants()
    for i in range(15):
        add_exam(db, i, "active", variants)
    add_exam(db, 99, "active", variants, started_at=datetime(2026, 11, 2, 8, 0, 0))
    db.commit()
    db.expunge_all()
    monkeypatch.setattr(main_module, "SessionLocal", lambda: _Borrowed(db))
    monkeypatch.setattr(router_module, "datetime", FrozenDatetime)

    with count_statements() as new:
        main_module._close_expired_exams_once()
    assert db.get(GeneratedExam, "EX099").status == "closed"
    assert all(db.get(GeneratedExam, f"EX{i:03d}").status == "active" for i in range(15))

    # the same sweep with 15 running exams and nothing to close: the old query read 15 x 280 KB, the new one nothing
    db.get(GeneratedExam, "EX099").status = "closed"
    db.commit()
    db.expunge_all()
    with count_statements() as quiet_new:
        main_module._close_expired_exams_once()
    db.expunge_all()
    monkeypatch.setattr(main_module, "_without_task_json", lambda q: q)
    with count_statements() as quiet_old:
        main_module._close_expired_exams_once()
    report("sweeper, 15 running exams, nothing to close", quiet_old, quiet_new)
    assert quiet_old.reading("generated_exams", "variants") == 1 and quiet_new.reading("generated_exams", "variants") == 0
    assert quiet_new.count == quiet_old.count


class _Borrowed:
    """The sweeper opens and closes its own Session; the test lends it the fixture's one and keeps it open."""
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        if name == "close":
            return lambda: None
        return getattr(self._db, name)
