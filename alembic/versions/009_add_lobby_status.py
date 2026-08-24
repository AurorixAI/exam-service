"""Add lobby status to generated_exams and waiting to exam_sessions

Revision ID: 009_lobby_status
Revises: 008_pinned_and_clone
Create Date: 2026-08-18
"""
from __future__ import annotations

from alembic import op

revision: str = "009_lobby_status"
down_revision = "008_pinned_and_clone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE generated_exams DROP CONSTRAINT IF EXISTS ck_generated_exams_status")
    op.execute("""
        ALTER TABLE generated_exams
        ADD CONSTRAINT ck_generated_exams_status
        CHECK (status IN ('draft', 'variants_ready', 'lobby', 'active', 'closed'))
    """)

    op.execute("ALTER TABLE exam_sessions DROP CONSTRAINT IF EXISTS ck_exam_sessions_status")
    op.execute("""
        ALTER TABLE exam_sessions
        ADD CONSTRAINT ck_exam_sessions_status
        CHECK (status IN ('waiting', 'in_progress', 'submitted', 'graded'))
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE generated_exams DROP CONSTRAINT IF EXISTS ck_generated_exams_status")
    op.execute("""
        ALTER TABLE generated_exams
        ADD CONSTRAINT ck_generated_exams_status
        CHECK (status IN ('draft', 'variants_ready', 'active', 'closed'))
    """)

    op.execute("ALTER TABLE exam_sessions DROP CONSTRAINT IF EXISTS ck_exam_sessions_status")
    op.execute("""
        ALTER TABLE exam_sessions
        ADD CONSTRAINT ck_exam_sessions_status
        CHECK (status IN ('in_progress', 'submitted', 'graded'))
    """)
