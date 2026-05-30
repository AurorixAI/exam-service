"""001 — exam service tables: generated_exams + exam_sessions

Revision ID: 001
Revises:
Create Date: 2025-01-01 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generated_exams",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("source_mode", sa.String(32), nullable=False, server_default="topics"),
        sa.Column("variants", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("skills_covered", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("toc_ids_used", postgresql.ARRAY(sa.Integer()), nullable=False, server_default="{}"),
        sa.Column("access_code", sa.String(16), nullable=True, unique=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("subject", sa.String(100), nullable=True),
        sa.Column("teacher_name", sa.String(200), nullable=True),
        sa.Column("total_time_minutes", sa.Integer(), nullable=False, server_default="45"),
        sa.Column("variants_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tasks_per_variant", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "exam_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("exam_id", sa.String(64), sa.ForeignKey("generated_exams.id"), nullable=False),
        sa.Column("student_name", sa.String(200), nullable=False),
        sa.Column("class_level", sa.Integer(), nullable=True),
        sa.Column("variant_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("answers", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("correct_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tasks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("time_spent_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="in_progress"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index("ix_exam_sessions_exam_id", "exam_sessions", ["exam_id"])
    op.create_index("ix_generated_exams_access_code", "generated_exams", ["access_code"])
    op.create_index("ix_generated_exams_status", "generated_exams", ["status"])


def downgrade() -> None:
    op.drop_table("exam_sessions")
    op.drop_table("generated_exams")
