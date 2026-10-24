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


def _twice(method, path):
    from src.main import app

    def fire(_):
        with TestClient(app, raise_server_exceptions=False) as client:
            return getattr(client, method)(f"{API}{path}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        return sorted(r.status_code for r in pool.map(fire, range(2)))


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
