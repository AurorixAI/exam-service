"""EX-20 on the real thing: two concurrent status changes on PostgreSQL (row lock), not the SQLite stand-in.

Skipped unless a throwaway Postgres is named (nothing else in the suite needs it):

    docker run -d --name exam-wa1-pg -e POSTGRES_USER=exam -e POSTGRES_PASSWORD=exam -e POSTGRES_DB=exam_wa1_t \
        -p 127.0.0.1:55442:5432 postgres:15-alpine
    EXAM_WA1_PG_URL=postgresql://exam:exam@127.0.0.1:55442/exam_wa1_t pytest tests/test_wa1_pg_double_click.py -q

The tables are created at the start and dropped at the end.  Before the fix both requests of a double click passed the
``status == 'lobby'`` check, both wrote ``started_at`` (the second moved the common timer) and both answered 200.
"""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api import router as router_module
from src.core.auth import require_exam_staff
from src.core.database import get_db
from src.models.exam_models import ExamLifecycleEvent, GeneratedExam
from src.models.shared import Base
from tests.conftest import TEST_GROUP_ID, TEST_LESSON_ID, TEST_ORG_ID, TEST_USER_ID, make_actor, make_task

PG_URL = os.getenv("EXAM_WA1_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set EXAM_WA1_PG_URL (a throwaway Postgres) to run")
API = "/api/v1"


@pytest.fixture
def pg(stub_auth_service, monkeypatch):
    from src.main import app

    engine = create_engine(PG_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def get_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = get_session
    app.dependency_overrides[require_exam_staff] = make_actor

    async def slow_lesson_check(exam, actor):  # the network wait in which the twin request overtakes
        await asyncio.sleep(0.5)

    monkeypatch.setattr(router_module, "_require_lesson_in_progress", slow_lesson_check)

    # the lookups of auth-service are the other network waits in which a twin request overtakes
    real_group, real_lesson = router_module.require_group_access, router_module._resolve_lesson_occurrence

    async def slow_group(actor, group_id):
        await asyncio.sleep(0.5)
        return await real_group(actor, group_id)

    async def slow_lesson(actor, **kwargs):
        await asyncio.sleep(0.5)
        return await real_lesson(actor, **kwargs)

    monkeypatch.setattr(router_module, "require_group_access", slow_group)
    monkeypatch.setattr(router_module, "_resolve_lesson_occurrence", slow_lesson)
    yield factory
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def _exam(factory, status, exam_id="EXAM_PG1"):
    with factory() as db:
        db.add(GeneratedExam(
            id=exam_id, title="PG", variants=[{"variant_number": 1, "tasks": [make_task()]}], settings={}, status=status,
            org_id=TEST_ORG_ID, created_by=TEST_USER_ID, group_id=TEST_GROUP_ID, group_name="8А", class_level=8,
            scheduled_lesson_id=TEST_LESSON_ID, scheduled_lesson_date=date.today().isoformat(), access_code="PGCODE01",
            skills_covered=[], toc_ids_used=[],
        ))
        db.commit()


def _twice_responses(method, path, **kwargs):
    from src.main import app

    def fire(_):
        with TestClient(app, raise_server_exceptions=False) as client:
            return getattr(client, method)(f"{API}{path}", **kwargs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(fire, range(2)))


def _twice(method, path, **kwargs):
    return sorted(r.status_code for r in _twice_responses(method, path, **kwargs))


def test_a_double_click_on_start_starts_the_exam_once(pg):
    _exam(pg, "lobby")
    assert _twice("post", "/exams/EXAM_PG1/start-live") == [200, 409]
    with pg() as db:
        assert db.get(GeneratedExam, "EXAM_PG1").status == "active"
        started = db.query(ExamLifecycleEvent).filter(ExamLifecycleEvent.event_type == "exam_started").count()
        assert started == 1


def test_a_double_click_on_archive_archives_once_and_deletes_nothing(pg):
    _exam(pg, "closed")
    assert _twice("delete", "/exams/EXAM_PG1") == [200, 200]
    with pg() as db:
        assert db.get(GeneratedExam, "EXAM_PG1").status == "archived"
        assert db.query(ExamLifecycleEvent).filter(ExamLifecycleEvent.event_type == "exam_archived").count() == 1


def test_a_double_click_on_restore_restores_once(pg):
    _exam(pg, "archived")
    assert _twice("post", "/exams/EXAM_PG1/restore") == [200, 409]
    with pg() as db:
        assert db.query(ExamLifecycleEvent).filter(ExamLifecycleEvent.event_type == "exam_restored").count() == 1


def test_a_double_click_on_delete_forever_leaves_one_trace(pg):
    from src.models.exam_models import ExamDeletion

    _exam(pg, "archived")
    codes = _twice("delete", "/exams/EXAM_PG1?permanent=true")
    assert codes == [200, 404]
    with pg() as db:
        assert db.get(GeneratedExam, "EXAM_PG1") is None
        assert db.query(ExamDeletion).count() == 1


def _events(factory, event_type):
    with factory() as db:
        return db.query(ExamLifecycleEvent).filter(ExamLifecycleEvent.event_type == event_type).count()


def _lobby_body():
    return {"lesson_id": TEST_LESSON_ID, "lesson_date": date.today().isoformat()}


@pytest.mark.parametrize(
    "path,payload",
    [
        ("open-lobby", _lobby_body()),
        ("publish", _lobby_body()),
        ("assign", {"group_id": TEST_GROUP_ID, "publish": True, **_lobby_body()}),
    ],
)
def test_a_double_click_on_opening_the_lobby_hands_both_clicks_the_same_code(pg, path, payload):
    """Without the lock both clicks drew their own code and the first teacher's QR pointed at a code that no longer existed."""
    _exam(pg, "variants_ready")
    with pg() as db:  # no code yet: each request would draw its own
        exam = db.get(GeneratedExam, "EXAM_PG1")
        exam.access_code, exam.scheduled_lesson_id, exam.scheduled_lesson_date = None, None, None
        db.commit()
    responses = _twice_responses("post", f"/exams/EXAM_PG1/{path}", json=payload)
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    codes = {r.json()["access_code"] for r in responses}
    assert len(codes) == 1
    with pg() as db:
        stored = db.get(GeneratedExam, "EXAM_PG1")
        assert stored.status == "lobby" and {stored.access_code} == codes
    assert _events(pg, "exam_lobby_opened") == 1  # the second click saw the opened lobby, not a draft


def test_a_double_click_on_prepare_creates_one_package(pg):
    _exam(pg, "variants_ready")
    with pg() as db:
        db.get(GeneratedExam, "EXAM_PG1").delivery_mode = "print"
        db.commit()
    body = {"group_id": TEST_GROUP_ID, **_lobby_body()}
    responses = _twice_responses("post", "/exams/EXAM_PG1/print-package/prepare", json=body)
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    refused = next(r for r in responses if r.status_code == 409)
    assert "другой открытый экзамен" not in refused.json()["detail"]
    with pg() as db:
        from src.models.exam_models import ExamPrintPackage

        assert db.query(ExamPrintPackage).count() == 1
    assert _events(pg, "print_package_prepared") == 1
