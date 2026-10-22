"""
Pytest fixtures for exam-service tests.

Uses SQLite in-memory DB — no running PostgreSQL needed.
Patches get_settings() so tests never touch real environment.
"""
from __future__ import annotations

import sys
import os
from datetime import date as _date

# Must be first — adds src/ to path before any src.* imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

# ── Patch settings BEFORE importing anything from src ──────────────────────
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("APP_ENV", "testing")

from src.core.config import Settings, get_settings
from src.models.shared import Base
from src.models.exam_models import ExamLifecycleEvent, GeneratedExam, ExamSession  # noqa: register tables

# ── SQLite compatibility shim (test-only) ──────────────────────────────────
# Production runs Postgres; the models use the Postgres ARRAY type
# (skills_covered / toc_ids_used). SQLite can neither create ARRAY columns nor
# bind a Python list to them. Swap ARRAY columns to a JSON-backed type on the
# in-memory test metadata BEFORE create_all — this fixes both DDL and
# bind/result (lists round-trip as JSON). Production metadata is untouched.
from sqlalchemy import JSON as _JSON
from sqlalchemy.dialects.postgresql import ARRAY as _PG_ARRAY

for _table in Base.metadata.tables.values():
    for _col in _table.columns:
        if isinstance(_col.type, _PG_ARRAY):
            _col.type = _JSON()


# ─── In-memory SQLite engine ────────────────────────────────────────────────

TEST_DB_URL = "sqlite:///:memory:"

engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
)

# Enable foreign keys in SQLite
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="session", autouse=True)
def create_tables():
    """Create all tables once per test session."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _fresh_abuse_limits():
    """Entry-rate counters are process-wide: every test starts (and leaves) them empty."""
    from src.core.abuse_limit import counters

    counters.reset()
    yield
    counters.reset()


@pytest.fixture
def db():
    """Fresh DB session per test, rolled back after."""
    connection = engine.connect()
    transaction = connection.begin()
    session = TestSessionLocal(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ─── Test actor ──────────────────────────────────────────────────────────────
# Every non-public exam endpoint depends on ``require_exam_staff``, which calls
# auth-service over HTTP (auth-service is the single source of truth for token
# validity and org role).  Tests must never do that, so the dependency is
# overridden with a fixed org owner.  Rows created by the factories below use
# the same org/user so ``require_exam_access`` lets them through.

TEST_USER_ID = "USR_TEST01"
TEST_ORG_ID = "ORG_TEST01"
# ``require_group_access`` rejects a non-UUID group id outright, so the factory
# group must look like a real auth-service group.
TEST_GROUP_ID = "11111111-2222-3333-4444-555555555555"
TEST_LESSON_ID = "LSN_TEST01"
# Per-attempt capability required by the public student endpoints.
TEST_SESSION_TOKEN = "test-session-capability-token"


def session_token_for(session_id: str) -> str:
    """Deterministic stand-in for the capability ``start`` issues.

    ``session_token`` is unique-indexed, so a test that creates several
    sessions must give each its own token and send the matching header.
    """
    return f"{TEST_SESSION_TOKEN}:{session_id}"


def session_headers(session_id: str) -> dict:
    return {"X-Exam-Session-Token": session_token_for(session_id)}


def make_actor(role: str = "teacher"):
    from src.core.auth import ExamActor

    return ExamActor(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        role=role,
        auth_headers={"authorization": "Bearer test-token"},
    )


@pytest.fixture
def actor():
    """The staff actor every ``client`` request is authenticated as."""
    return make_actor()


TEST_GROUP = {
    "id": TEST_GROUP_ID,
    "name": "8А",
    "class_level": 8,
    "teacher_id": TEST_USER_ID,
    "teacher_name": "Айгуль Серикова",
}


@pytest.fixture
def stub_auth_service(monkeypatch):
    """Neutralise every outbound auth-service call.

    exam-service deliberately keeps no copy of the auth state: group access,
    schedule occurrences and the student roster are all resolved over HTTP
    against auth-service.  Tests must never make those calls (they would fail
    closed with 503), so each helper answers from a canonical in-memory group.
    """
    from fastapi import HTTPException
    from src.api import router
    from src.core import auth as core_auth

    async def fake_require_group_access(actor, group_id):
        if str(group_id) != TEST_GROUP_ID:
            raise HTTPException(404, "Группа не найдена")
        return dict(TEST_GROUP)

    async def fake_resolve_lesson_occurrence(
        actor, *, group_id, lesson_id, lesson_date, require_current_or_future=True, substitute_bound_lesson=None
    ):
        group = await fake_require_group_access(actor, group_id)
        return group, {
            "id": lesson_id,
            "groupId": group_id,
            "occurrenceDate": lesson_date,
            "subject": "Математика",
        }

    async def fake_require_public_lobby_available(exam):
        return None

    async def fake_require_lesson_in_progress(exam, actor):
        return None

    async def fake_canonical_student(exam, *, student_id, student_name, identifier=None):
        name = student_name or identifier or student_id or "Ученик"
        return {
            "id": student_id or f"STU_{abs(hash(name)) % 10**8}",
            "display_id": identifier,
            "full_name": name,
            "class_level": TEST_GROUP["class_level"],
        }

    async def fake_require_student_history_access(actor, student_id):
        return {"id": str(student_id), "org_id": TEST_ORG_ID}

    monkeypatch.setattr(core_auth, "require_group_access", fake_require_group_access)
    monkeypatch.setattr(router, "require_group_access", fake_require_group_access)
    monkeypatch.setattr(
        core_auth,
        "require_student_history_access",
        fake_require_student_history_access,
    )
    monkeypatch.setattr(
        router,
        "require_student_history_access",
        fake_require_student_history_access,
    )
    monkeypatch.setattr(router, "_resolve_lesson_occurrence", fake_resolve_lesson_occurrence)
    monkeypatch.setattr(
        router, "_require_public_lobby_available", fake_require_public_lobby_available
    )
    monkeypatch.setattr(router, "_require_lesson_in_progress", fake_require_lesson_in_progress)
    monkeypatch.setattr(router, "_canonical_student_for_exam", fake_canonical_student)


@pytest.fixture
def client(db, stub_auth_service):
    """TestClient with DB, staff-auth and auth-service calls stubbed out."""
    from src.main import app
    from src.core.database import get_db
    from src.core.auth import require_exam_staff, require_heavy_staff

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_exam_staff] = make_actor
    app.dependency_overrides[require_heavy_staff] = make_actor  # the report / PDF routes: same actor, no slot
    # Student endpoints require the per-attempt capability issued by ``start``.
    # Factory sessions all carry ``TEST_SESSION_TOKEN``, so sending it by
    # default keeps every request authorised; a test that wants to exercise the
    # guard overrides the header explicitly.
    with TestClient(
        app,
        raise_server_exceptions=True,
        headers={"X-Exam-Session-Token": TEST_SESSION_TOKEN},
    ) as c:
        yield c
    app.dependency_overrides.clear()


# ─── Reusable data factories ─────────────────────────────────────────────────

def make_task(
    task_id: str = "TASK_001",
    skill_id: str = "SK_001",
    skill_name: str = "Линейные уравнения",
    difficulty: str = "B",
    text: str = "Решите уравнение: 2x + 4 = 10",
    answer: str = "3",
    answer_type: str = "exact_number",
    answer_options: list = None,
) -> dict:
    return {
        "task_id": task_id,
        "skill_id": skill_id,
        "skill_name": skill_name,
        "difficulty": difficulty,
        "question_text": text,
        "question_latex": None,
        "question_image_url": None,
        "answer_type": answer_type,
        "correct_answer": answer,
        "answer_options": answer_options,
        "distractor_meta": None,
        "is_template_generated": False,
    }


def make_exam_in_db(db, tasks: list = None, status: str = "active") -> GeneratedExam:
    if tasks is None:
        tasks = [make_task()]
    exam = GeneratedExam(
        id="EXAM_TEST01",
        title="Тестовый экзамен",
        source_mode="topics",
        variants=[{"variant_number": 1, "tasks": tasks}],
        settings={"time_minutes": 45, "mc_mode": "auto"},
        skills_covered=["SK_001"],
        toc_ids_used=[],
        status=status,
        total_time_minutes=45,
        variants_count=1,
        tasks_per_variant=len(tasks),
        org_id=TEST_ORG_ID,
        created_by=TEST_USER_ID,
        # An online exam is only publicly reachable once it is bound to one
        # concrete lesson occurrence (see ``_require_bound_exam``); without
        # this binding every by-code request answers 410.
        group_id=TEST_GROUP_ID,
        group_name="8А",
        class_level=8,
        scheduled_lesson_id=TEST_LESSON_ID,
        scheduled_lesson_date=_date.today().isoformat(),
    )
    db.add(exam)
    db.commit()
    db.refresh(exam)
    return exam


def make_session_in_db(db, exam: GeneratedExam, status: str = "in_progress") -> ExamSession:
    sess = ExamSession(
        id="SES_TEST01",
        exam_id=exam.id,
        student_name="Иван Иванов",
        variant_number=1,
        answers={},
        max_score=len(exam.variants[0]["tasks"]) * 10,
        status=status,
        session_token=TEST_SESSION_TOKEN,
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess
