"""Add archived status to generated_exams check constraint

Revision ID: 010_archived_status
Revises: 009_lobby_status
Create Date: 2026-08-18
"""
from __future__ import annotations

from alembic import op

revision: str = "010_archived_status"
down_revision = "009_lobby_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE generated_exams DROP CONSTRAINT IF EXISTS ck_generated_exams_status")
    op.execute("""
        ALTER TABLE generated_exams
        ADD CONSTRAINT ck_generated_exams_status
        CHECK (status IN ('draft', 'variants_ready', 'lobby', 'active', 'closed', 'archived'))
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE generated_exams DROP CONSTRAINT IF EXISTS ck_generated_exams_status")
    op.execute("""
        ALTER TABLE generated_exams
        ADD CONSTRAINT ck_generated_exams_status
        CHECK (status IN ('draft', 'variants_ready', 'lobby', 'active', 'closed'))
    """)
