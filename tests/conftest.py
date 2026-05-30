"""
Pytest fixtures for exam-service tests.

Uses SQLite in-memory DB — no running PostgreSQL needed.
Patches get_settings() so tests never touch real environment.
"""
from __future__ import annotations

import sys
import os

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
from src.models.exam_models import GeneratedExam, ExamSession  # noqa: register tables


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


@pytest.fixture
def client(db):
    """TestClient with DB dependency overridden."""
    from src.main import app
    from src.core.database import get_db

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
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
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess
