"""Add CHECK constraints and org_id to exam tables

- generated_exams: add CHECK on status, source_mode; add org_id column
- exam_sessions:   add CHECK on status

Revision ID: 002_add_checks_and_org
Revises: 001
Create Date: 2026-05-27
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "002_add_checks_and_org"
down_revision = "001"
branch_labels = None
depends_on = None

# ── valid enum values ──────────────────────────────────────────────────────────
_EXAM_STATUS   = ("draft", "published", "archived")
_SOURCE_MODES  = ("topics", "textbook", "manual")
_SESSION_STATUS = ("in_progress", "submitted", "graded")


def upgrade() -> None:
    # generated_exams — status CHECK
    op.create_check_constraint(
        "ck_generated_exams_status",
        "generated_exams",
        sa.text(f"status IN {_EXAM_STATUS!r}".replace("[", "(").replace("]", ")")),
    )

    # generated_exams — source_mode CHECK
    op.create_check_constraint(
        "ck_generated_exams_source_mode",
        "generated_exams",
        sa.text(f"source_mode IN {_SOURCE_MODES!r}".replace("[", "(").replace("]", ")")),
    )

    # generated_exams — add org_id (nullable so existing rows are unaffected)
    op.add_column(
        "generated_exams",
        sa.Column("org_id", sa.String(36), nullable=True),
    )
    op.create_index("idx_ge_org_id", "generated_exams", ["org_id"])

    # exam_sessions — status CHECK
    op.create_check_constraint(
        "ck_exam_sessions_status",
        "exam_sessions",
        sa.text(f"status IN {_SESSION_STATUS!r}".replace("[", "(").replace("]", ")")),
    )


def downgrade() -> None:
    op.drop_constraint("ck_exam_sessions_status", "exam_sessions", type_="check")
    op.drop_index("idx_ge_org_id", table_name="generated_exams")
    op.drop_column("generated_exams", "org_id")
    op.drop_constraint("ck_generated_exams_source_mode", "generated_exams", type_="check")
    op.drop_constraint("ck_generated_exams_status", "generated_exams", type_="check")
