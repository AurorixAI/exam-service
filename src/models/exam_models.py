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
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB as PostgreSQLJSONB
from sqlalchemy.sql import func

from src.models.shared import Base

# Keep SQLite-based unit fixtures usable, while accurately modelling the
# JSONB columns that production PostgreSQL stores.  A plain ``JSON`` model
# here makes Alembic propose a destructive JSONB → JSON migration on every
# check, even though the database is already correct.
JSONB = JSON().with_variant(PostgreSQLJSONB(), "postgresql")


class GeneratedExam(Base):
    """
    A teacher-created exam configuration.

    Lifecycle: draft → variants_ready → active → closed

    Library flow:
      - Exams with group_id=None are "templates" living in the teacher's Library.
      - is_pinned=True exams appear at the top of the Library.
      - source_exam_id tracks the original template when an exam is cloned.
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
    # Mandatory organisation boundary for all new exams.  Existing unscoped
    # rows remain invisible until an explicit migration assigns their owner.
    org_id = Column(String(36), nullable=True, index=True)
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

    # Group binding (nullable = unassigned / library template)
    group_id = Column(String(36), nullable=True, index=True)
    group_name = Column(String(200), nullable=True)
    class_level = Column(Integer, nullable=True)

    # A published exam is tied to one *concrete* occurrence of a lesson, not
    # merely to a recurring group schedule.  The occurrence identity is the
    # stable pair used by auth-service: schedule-slot id + YYYY-MM-DD date.
    # A one-off time/room transfer keeps this pair unchanged, so the exam
    # naturally follows the same lesson without any duplicate update.
    scheduled_lesson_id = Column(String(100), nullable=True)
    scheduled_lesson_date = Column(String(10), nullable=True)

    # Library features
    is_pinned = Column(Boolean, nullable=False, default=False)
    source_exam_id = Column(String(30), nullable=True)  # set when cloned

    created_by = Column(String(100))   # user_id from auth
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index(
            "idx_generated_exams_lesson_binding",
            "org_id",
            "scheduled_lesson_id",
            "scheduled_lesson_date",
        ),
    )


class ExamLifecycleEvent(Base):
    """Append-only operational history of one exam.

    ``GeneratedExam.status`` describes only the state *now*.  This table keeps
    the facts needed by the administrator's historical monitor: when a run
    was bound, moved, opened, started, closed or released after a cancelled
    lesson.  It never stores exam tasks, answer keys or a student capability.
    """

    __tablename__ = "exam_lifecycle_events"

    id = Column(String(36), primary_key=True)
    exam_id = Column(String(30), ForeignKey("generated_exams.id", ondelete="CASCADE"), nullable=False)
    org_id = Column(String(36), nullable=False)
    event_type = Column(String(50), nullable=False)
    occurred_at = Column(DateTime, nullable=False, server_default=func.now())

    # Context is copied at the time of the event, so a later group rename or
    # a lesson reschedule cannot rewrite the historical record.
    actor_id = Column(String(100), nullable=True)
    group_id = Column(String(36), nullable=True)
    group_name = Column(String(200), nullable=True)
    lesson_id = Column(String(100), nullable=True)
    lesson_date = Column(String(10), nullable=True)
    previous_lesson_id = Column(String(100), nullable=True)
    previous_lesson_date = Column(String(10), nullable=True)
    reason = Column(String(500), nullable=True)
    details = Column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("idx_exam_lifecycle_org_time", "org_id", "occurred_at"),
        Index("idx_exam_lifecycle_exam_time", "exam_id", "occurred_at"),
        Index("ix_exam_lifecycle_events_event_type", "event_type"),
    )


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

    student_id = Column(String(36), nullable=True, index=True)
    display_id = Column(String(50), nullable=True, index=True)
    # Opaque capability returned only to the student who starts this attempt.
    # A short session id alone must never authorise progress/submit/results.
    session_token = Column(String(128), nullable=True, unique=True, index=True)
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
