"""
Exam Service — Owned ORM models.

Tables created and managed exclusively by exam-service Alembic migrations.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy import JSON as JSONB
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.sql import func

from src.models.shared import Base


class GeneratedExam(Base):
    """
    A teacher-created exam configuration.

    Lifecycle: draft → active → closed
    """
    __tablename__ = "generated_exams"

    id = Column(String(30), primary_key=True)
    title = Column(String(300), nullable=False)

    # Source mode: "topics" (by skill_id) | "textbook" (by toc_id)
    source_mode = Column(String(20), nullable=False, default="topics")

    # Full config + task pool stored as JSONB
    # variants: List[{variant_number, tasks: [task_dict]}]
    variants = Column(JSONB, nullable=False, default=list)

    # Flat exam-level settings (time, shuffle, mc_mode, etc.)
    settings = Column(JSONB, nullable=False, default=dict)

    # Coverage metadata — matches migration 001 (ARRAY types)
    skills_covered = Column(ARRAY(String), nullable=False, server_default="{}")
    toc_ids_used = Column(ARRAY(Integer), nullable=False, server_default="{}")

    # Access
    access_code = Column(String(20), unique=True)
    status = Column(String(20), nullable=False, default="draft")
    # draft | active | closed

    # Display
    subject = Column(String(100), default="Математика")
    teacher_name = Column(String(200))

    # Stats
    total_time_minutes = Column(Integer, default=45)
    variants_count = Column(Integer, default=1)
    tasks_per_variant = Column(Integer, default=10)

    created_by = Column(String(100))   # user_id from auth
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ExamReport(Base):
    """
    On-demand LLM narrative report for a whole exam (one per exam).

    Generation flow:
      POST /exams/{exam_id}/report  →  aggregate sessions → Gemini → persist here
      GET  /exams/{exam_id}/report  →  return cached row

    Force refresh: POST /exams/{exam_id}/report?force=true (overwrites)
    """
    __tablename__ = "exam_reports"

    id = Column(String(30), primary_key=True)
    exam_id = Column(String(30), ForeignKey("generated_exams.id"), nullable=False, unique=True)

    # Snapshot of aggregated stats at generation time
    total_students = Column(Integer, default=0)
    submitted_count = Column(Integer, default=0)
    avg_score_pct = Column(Numeric(5, 2), default=0)
    pass_rate_pct = Column(Numeric(5, 2), default=0)

    # Aggregated analytics snapshots (mirrors analytics endpoint output)
    task_analytics = Column(JSONB, nullable=True)   # List[{question_preview, skill_name, correct_rate, ...}]
    top_errors = Column(JSONB, nullable=True)        # List[{error_type, explanation, count, ...}]

    # LLM narrative
    narrative_ru = Column(Text, nullable=True)
    narrative_generated = Column(Boolean, default=False)

    # State
    is_final = Column(Boolean, default=False)       # True when exam is closed
    generated_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ExamSession(Base):
    """
    One student's attempt at a GeneratedExam variant.
    """
    __tablename__ = "exam_sessions"

    id = Column(String(30), primary_key=True)
    exam_id = Column(String(30), ForeignKey("generated_exams.id"), nullable=False)

    student_name = Column(String(200), nullable=False)
    class_level = Column(Integer)
    variant_number = Column(Integer, default=1)

    # {task_id: answer_string}
    answers = Column(JSONB, nullable=False, default=dict)

    score = Column(Integer, default=0)
    max_score = Column(Integer, default=0)
    correct_count = Column(Integer, default=0)
    total_tasks = Column(Integer, default=0)

    time_spent_seconds = Column(Integer, default=0)

    # {task_id: {hit: bool, distractor_value: str, error_type: str, explanation: str}}
    distractor_analytics = Column(JSONB, nullable=True, default=None)

    status = Column(String(20), nullable=False, default="in_progress")
    # in_progress | submitted

    started_at = Column(DateTime, server_default=func.now())
    submitted_at = Column(DateTime)
