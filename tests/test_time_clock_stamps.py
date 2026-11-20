"""TIME-TESTS (P2, round 2): ONE time base inside the service.  The stand shifts the business day AND the stamps and timers together, so a shifted day is
never compared with a real ``created_at`` / ``occurred_at`` / ``started_at``.

* guard: no real-clock call in ``src`` outside ``src/core/business_clock.py`` (the allowed exceptions are listed, each with its reason);
* behaviour: with the offset +1 day an exam created now shows in the monitor filtered by the business «today» (= tomorrow); the timer of a running exam
  runs out when the offset grows by its duration; outside the mode the stamps are the real time and Redis is never touched."""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.api import router as router_module
from src.core import business_clock as bc
from src.models.exam_models import GeneratedExam
from tests.conftest import TEST_ORG_ID, business_today, make_actor, make_exam_in_db, make_task
from tests.test_time_clock import CountingRedis

SRC = Path(__file__).resolve().parents[1] / "src"
FORBIDDEN = {("datetime", "utcnow"), ("datetime", "now"), ("date", "today"), ("datetime", "today"), ("time", "time")}
# (file relative to src, call) -> reason.  The clock module is the one place that reads the real clock.
ALLOWED: dict[tuple[str, str], str] = {}  # signature / token expiry: the service has none (it trusts auth-service); nothing else is excused


def _real_clock_calls(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = ast.unparse(node.func.value).split(".")[-1]
            if (owner, node.func.attr) in FORBIDDEN:
                yield node.lineno, f"{owner}.{node.func.attr}()"


def test_no_real_clock_read_in_src_outside_the_clock_module():
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == "core/business_clock.py":
            continue
        for line, call in _real_clock_calls(path):
            if (rel, call) not in ALLOWED:
                found.append(f"{rel}:{line} {call}")
    assert not found, "read the time through src.core.business_clock: " + ", ".join(found)


def test_the_guard_sees_a_violation():
    tree = "import datetime\nx = datetime.datetime.utcnow()\ny = time.time()\n"
    path = SRC.parent / "tests" / "_guard_probe.py"
    path.write_text(tree, encoding="utf-8")
    try:
        assert [call for _, call in _real_clock_calls(path)] == ["datetime.utcnow()", "time.time()"]
    finally:
        path.unlink()


def test_model_stamp_defaults_come_from_the_clock_module_with_the_type_of_the_column():
    from src.models import exam_models

    checked = 0
    for table in exam_models.Base.metadata.tables.values():
        for column in table.columns:
            if column.server_default is None or "now" not in str(column.server_default.arg).lower():
                continue
            assert column.default is not None, (table.name, column.name)  # a Python default; server_default stays as the fallback
            value = column.default.arg(None)
            assert (value.tzinfo is not None) == bool(column.type.timezone), (table.name, column.name)  # aware for timestamptz, naive otherwise
            assert abs((value.replace(tzinfo=None) - bc.utc_now()).total_seconds()) < 1
            checked += 1
    assert checked >= 8


def test_the_four_timestamptz_columns_are_declared_with_a_zone():
    """migration 001: exam_sessions.started_at / submitted_at and generated_exams.created_at / updated_at are ``timestamp with time zone``."""
    from src.models.exam_models import ExamSession, GeneratedExam

    for column in (ExamSession.started_at, ExamSession.submitted_at, GeneratedExam.created_at, GeneratedExam.updated_at):
        assert column.type.timezone is True, column
    from src.models.exam_models import ExamLifecycleEvent, ExamReport

    for column in (ExamLifecycleEvent.occurred_at, ExamReport.updated_at, ExamReport.generated_at):
        assert column.type.timezone is False, column


def test_aware_and_naive_stamps_of_the_clock_module_differ_only_by_the_zone():
    naive, aware = bc.utc_now(), bc.utc_now_aware()
    assert naive.tzinfo is None and aware.utcoffset() == timedelta(0)
    assert abs((aware.replace(tzinfo=None) - naive).total_seconds()) < 1


class _Rows:
    """A query chain whose first() returns the given row (what PostgreSQL returns for ``timestamptz``: AWARE values)."""

    def __init__(self, row):
        self.row = row

    def __getattr__(self, name):
        return lambda *a, **k: self

    def first(self):
        return self.row


@pytest.mark.parametrize("shift", [0, 5400])
def test_aware_started_at_from_postgres_does_not_break_the_timers(stand, shift):
    """exam_sessions.started_at as PostgreSQL returns it (aware): the timers subtract it from the stand clock without a TypeError."""
    from types import SimpleNamespace

    _shift(stand, shift)
    started = bc.utc_now_aware() - timedelta(minutes=10)
    exam = SimpleNamespace(id="E", settings={}, status="active", total_time_minutes=45)
    assert router_module._exam_started_at(_Rows((started,)), exam).tzinfo is None
    assert router_module._exam_deadline_passed(_Rows((started,)), exam) is False
    assert 34 * 60 <= router_module._exam_remaining_seconds(_Rows((started,)), exam) <= 35 * 60
    session = SimpleNamespace(started_at=started)
    total, left = router_module._remaining_seconds(exam, session, None, bc.utc_now())
    assert total == 45 * 60 and 34 * 60 <= left <= 35 * 60
    old = bc.utc_now_aware() - timedelta(minutes=50)
    assert router_module._exam_deadline_passed(_Rows((old,)), exam) is True


def test_aware_submitted_at_and_report_stamps_do_not_break_the_report_cooldown(stand):
    """exam_sessions.submitted_at (aware from PostgreSQL) is compared with the report's stamps and the cool-down is counted on the stand clock."""
    from types import SimpleNamespace

    from src.domain import report_generator

    _shift(stand, 7200)
    now = bc.utc_now_aware()
    report = SimpleNamespace(updated_at=bc.utc_now(), generated_at=None, avg_score_pct=50, pass_rate_pct=50)
    assert report_generator._last_generated_at(report).tzinfo is None
    assert report_generator._naive_utc(now - timedelta(minutes=5)) < report_generator._last_generated_at(report)
    assert abs((report_generator._utcnow() - bc.utc_now()).total_seconds()) < 1


@pytest.fixture
def stand(monkeypatch):
    fake = CountingRedis("0")
    monkeypatch.setattr(bc, "TEST_CLOCK_MODE", True)
    monkeypatch.setattr(bc, "_redis_client", fake)
    monkeypatch.setattr(bc, "_offset_cache", (0, 0.0))
    return fake


def _shift(stand, seconds):
    stand.value = str(seconds)
    bc._offset_cache = (0, 0.0)


def test_an_exam_created_now_is_in_the_monitor_of_the_shifted_today(stand, client, db):
    from src.core.auth import require_exam_staff
    from src.main import app

    app.dependency_overrides[require_exam_staff] = lambda: make_actor("admin")
    real_today = business_today()
    _shift(stand, 86400)
    tomorrow = router_module._business_today()
    assert tomorrow == real_today + timedelta(days=1)
    fresh = GeneratedExam(id="FRESH", title="fresh", source_mode="topics", variants=[{"variant_number": 1, "tasks": [make_task()]}], settings={"time_minutes": 45},
                          skills_covered=["SK_001"], toc_ids_used=[], status="lobby", total_time_minutes=45, variants_count=1, tasks_per_variant=1,
                          org_id=TEST_ORG_ID, created_by="u")
    db.add(fresh)
    db.commit()
    db.refresh(fresh)
    assert abs((fresh.created_at - bc.utc_now()).total_seconds()) < 5  # the stamp is on the stand clock
    answer = client.get("/api/v1/admin/exams/monitor", params={"date_from": tomorrow.isoformat(), "date_to": tomorrow.isoformat()})
    assert answer.status_code == 200, answer.text
    assert "FRESH" in [item["exam_id"] for item in answer.json()["items"]]


def test_the_timer_of_a_running_exam_runs_out_on_the_stand_clock(stand, db):
    _shift(stand, 3 * 86400)
    exam = make_exam_in_db(db, status="active")
    exam.total_time_minutes = 45
    exam.settings = {"time_minutes": 45, "started_at": bc.utc_now().isoformat()}
    db.commit()
    assert router_module._exam_deadline_passed(db, exam) is False
    assert 44 * 60 <= router_module._exam_remaining_seconds(db, exam) <= 45 * 60
    _shift(stand, 3 * 86400 + 45 * 60 + 60)  # the stand clock moves on by the duration of the exam plus a minute
    assert router_module._exam_deadline_passed(db, exam) is True
    assert router_module._exam_remaining_seconds(db, exam) == 0


def test_off_mode_stamps_are_the_real_time_and_redis_is_not_touched(db, monkeypatch):
    fake = CountingRedis("999999")
    monkeypatch.setattr(bc, "_redis_client", fake)
    assert bc.TEST_CLOCK_MODE is False
    real = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((bc.utc_now() - real).total_seconds()) < 1 and bc.utc_now().tzinfo is None
    exam = make_exam_in_db(db, status="lobby")
    db.refresh(exam)
    assert abs((exam.created_at - real).total_seconds()) < 5 and abs((exam.updated_at - real).total_seconds()) < 5
    assert abs((router_module._business_now().astimezone(timezone.utc).replace(tzinfo=None) - real).total_seconds()) < 1
    assert fake.calls == 0
