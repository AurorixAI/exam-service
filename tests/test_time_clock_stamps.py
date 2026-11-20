"""TIME-TESTS (P2, round 2): ONE time base inside the service.  The stand shifts the business day AND the stamps and timers together, so a shifted day is
never compared with a real ``created_at`` / ``occurred_at`` / ``started_at``.

* guard: no real-clock call in ``src`` outside ``src/core/business_clock.py`` (the allowed exceptions are listed, each with its reason);
* behaviour: with the offset +1 day an exam created now shows in the monitor filtered by the business «today» (= tomorrow); the timer of a running exam
  runs out when the offset grows by its duration; outside the mode the stamps are the real time and Redis is never touched."""
from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import DateTime

from src.api import router as router_module
from src.core import business_clock as bc
from src.models.exam_models import GeneratedExam
from tests.conftest import TEST_ORG_ID, business_today, make_actor, make_exam_in_db, make_task
from tests.test_time_clock import CountingRedis

SRC = Path(__file__).resolve().parents[1] / "src"
CLOCK_MODULE = "core/business_clock.py"
# Wall-clock readers.  ``time.monotonic`` / ``perf_counter`` are NOT here: they measure an interval, not a moment.
DT_ATTRS = {"utcnow", "now", "today", "utcfromtimestamp", "fromtimestamp"}
TIME_FUNCS = {"time", "time_ns", "gmtime", "localtime", "strftime", "ctime", "asctime"}
SQL_NOW_FUNCS = {"now", "current_timestamp", "current_date", "localtimestamp", "statement_timestamp", "clock_timestamp", "transaction_timestamp"}
SQL_NOW_RE = re.compile(r"\bnow\s*\(\s*\)|\b(?:current_timestamp|current_date|localtimestamp|statement_timestamp|clock_timestamp|transaction_timestamp)\b", re.I)
# (file relative to src, kind) -> reason.  The clock module is the one place that reads the real clock; nothing else is excused.
ALLOWED: dict[tuple[str, str], str] = {}


def _scan(source: str):
    """[(line, kind, text)] of every way the source can read the wall clock (outside the clock module) or let the database do it.

    kinds: ``call`` (``datetime.utcnow()``, ``dt.utcnow()``, ``time.time()``, ``from time import time; time()``), ``ref`` (a clock function used
    WITHOUT a call: ``default=datetime.utcnow``), ``sql-func`` (``func.now()`` in a query or a column; a ``server_default`` of a model column is the
    declared fallback and is skipped), ``sql-text`` (``text("... now() ...")``)."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    time_names, from_time, dt_classes, dt_modules = {"time"}, {}, {"datetime", "date"}, {"datetime"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "time":
                    time_names.add(a.asname or "time")
                elif a.name == "datetime":
                    dt_modules.add(a.asname or "datetime")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "time":
                for a in node.names:
                    if a.name in TIME_FUNCS:
                        from_time[a.asname or a.name] = a.name
            elif node.module == "datetime":
                for a in node.names:
                    if a.name in {"datetime", "date"}:
                        dt_classes.add(a.asname or a.name)

    def in_server_default(node):
        while node in parents:
            parent = parents[node]
            if isinstance(parent, ast.keyword) and parent.arg == "server_default":
                return True
            node = parent
        return False

    def clock_reader(node):
        """The wall-clock reader that a Name / Attribute denotes, or None."""
        if isinstance(node, ast.Attribute):
            owner = ast.unparse(node.value)
            if node.attr in TIME_FUNCS and owner in time_names:
                return f"time.{node.attr}"
            if node.attr in DT_ATTRS and (owner.split(".")[-1] in dt_classes or owner.split(".")[-1] == "date"):
                return f"{owner}.{node.attr}"
            if node.attr in {"utcnow", "utcfromtimestamp"}:  # any alias of a datetime class: ``dt.utcnow()``
                return f"{owner}.{node.attr}"
        elif isinstance(node, ast.Name):
            if node.id in from_time:
                return f"time.{from_time[node.id]}"
            if node.id in {"utcnow", "utcfromtimestamp"}:
                return node.id
        return None

    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Attribute, ast.Name)):
            what = clock_reader(node)
            parent = parents.get(node)
            if what and not isinstance(parent, (ast.ImportFrom, ast.alias)) and not (isinstance(parent, ast.Attribute) and clock_reader(parent)):
                found.add((node.lineno, "call" if isinstance(parent, ast.Call) and parent.func is node else "ref", what))
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in SQL_NOW_FUNCS and ast.unparse(f.value).split(".")[-1] in {"func", "sqlfunc"}:
                if not in_server_default(node):
                    found.add((node.lineno, "sql-func", ast.unparse(node)))
            elif (isinstance(f, ast.Attribute) and f.attr == "text") or (isinstance(f, ast.Name) and f.id == "text"):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    if SQL_NOW_RE.search(node.args[0].value) and not in_server_default(node):
                        found.add((node.lineno, "sql-text", node.args[0].value[:60]))
    return sorted(found)


def test_no_real_clock_read_in_src_outside_the_clock_module():
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == CLOCK_MODULE:
            continue
        for line, kind, what in _scan(path.read_text(encoding="utf-8")):
            if (rel, kind) not in ALLOWED:
                found.append(f"{rel}:{line} {kind} {what}")
    assert not found, "read the time through src.core.business_clock: " + ", ".join(found)


@pytest.mark.parametrize("source, kind", [
    ("import datetime\nx = datetime.datetime.utcnow()\n", "call"),
    ("from datetime import datetime\nx = datetime.now(timezone.utc)\n", "call"),
    ("from datetime import date\nx = date.today()\n", "call"),
    ("import time\nx = time.time()\n", "call"),
    ("from time import time\nx = time()\n", "call"),
    ("from time import time as clock\nx = clock()\n", "call"),
    ("import datetime as dt\nx = dt.datetime.utcnow()\n", "call"),
    ("from datetime import datetime as dt\nx = dt.utcnow()\n", "call"),
    ("from datetime import datetime\nc = Column(DateTime, default=datetime.utcnow)\n", "ref"),
    ("import time\nf = time.time\n", "ref"),
    ("import time\nx = time.time_ns()\n", "call"),
    ("import time\nx = time.gmtime()\n", "call"),
    ("import time\nx = time.localtime()\n", "call"),
    ("import time\nx = time.strftime('%Y')\n", "call"),
    ("from time import gmtime\nx = gmtime()\n", "call"),
    ("q = db.query(X).filter(X.at < func.now())\n", "sql-func"),
    ("q = db.query(sqlfunc.now())\n", "sql-func"),
    ("q = func.current_timestamp()\n", "sql-func"),
    ('db.execute(text("UPDATE t SET at = now() WHERE id = 1"))\n', "sql-text"),
    ('db.execute(sqlalchemy.text("SELECT CURRENT_TIMESTAMP"))\n', "sql-text"),
    ("c = Column(DateTime, onupdate=func.now())\n", "sql-func"),
])
def test_the_guard_catches_every_kind_of_violation(source, kind):
    assert kind in [k for _, k, _ in _scan(source)], source


@pytest.mark.parametrize("source", [
    "import time\nx = time.monotonic() + time.perf_counter()\n",
    "from datetime import datetime\nx = datetime(2026, 1, 1) + datetime.fromisoformat('2026-01-01')\n",
    "c = Column(DateTime, default=utc_now, server_default=func.now())\n",
    "c = Column(DateTime, server_default=text('now()'))\n",
    'db.execute(text("SELECT pg_advisory_lock(:k)"))\n',
    "x = business_clock.utc_now()\n",
    "import asyncio\nawait asyncio.sleep(3)\n",
])
def test_the_guard_lets_a_clean_source_pass(source):
    assert _scan(source) == [], source


def test_the_clock_module_reads_the_real_time_in_one_place_only():
    """``_real_now`` is the only function of the clock module that touches the wall clock; every other reader goes through it."""
    source = (SRC / CLOCK_MODULE).read_text(encoding="utf-8")
    tree = ast.parse(source)
    readers = set()
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if any(kind in {"call", "ref"} for _, kind, _ in _scan(ast.get_source_segment(source, fn).lstrip())):
            readers.add(fn.name)
    assert readers == {"_real_now"}, readers


def test_the_single_point_pins_every_reader_of_the_clock(monkeypatch):
    moment = datetime(2026, 11, 2, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(bc, "_real_now", lambda: moment)
    assert bc.utc_now() == datetime(2026, 11, 2, 9, 30) and bc.utc_now().tzinfo is None
    assert bc.utc_now_aware() == moment and bc.utc_now_aware().utcoffset() == timedelta(0)
    local = bc.business_now(ZoneInfo("Asia/Tashkent"))
    assert (local.hour, local.minute) == (14, 30) and local.utcoffset() == timedelta(hours=5)
    assert router_module._business_today().isoformat() == "2026-11-02"


def _all_datetime_columns():
    from src.models import exam_models

    for table in exam_models.Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, DateTime):
                yield table.name, column


def test_every_datetime_column_stamps_from_the_clock_module_with_the_type_of_the_column():
    """ALL ``DateTime`` columns, not only those with a ``server_default``: a Python default must come from the clock module and match the zone of the
    column; an ``onupdate`` too."""
    checked = 0
    for table, column in _all_datetime_columns():
        if column.default is None:
            continue
        func = column.default.arg
        assert getattr(func, "__module__", "") == "src.core.business_clock", (table, column.name, func)
        value = func(None)
        assert (value.tzinfo is not None) == bool(column.type.timezone), (table, column.name)
        assert abs((value.replace(tzinfo=None) - bc.utc_now()).total_seconds()) < 1
        if column.onupdate is not None:
            assert getattr(column.onupdate.arg, "__module__", "") == "src.core.business_clock", (table, column.name)
        checked += 1
    assert checked >= 8


def test_a_datetime_column_without_a_python_default_has_no_database_clock_either():
    """No Python default and a ``server_default`` now() would let the database read ITS clock instead of the stand clock."""
    for table, column in _all_datetime_columns():
        if column.default is None:
            assert column.server_default is None, (table, column.name)


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
