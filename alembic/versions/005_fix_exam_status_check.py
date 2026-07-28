"""Fix generated_exams status CHECK constraint to match app code

002_add_checks_and_org created ck_generated_exams_status allowing only
('draft', 'published', 'archived'), but the repository code has always
written ('draft', 'variants_ready', 'active', 'closed') — see
src/repositories/exam_repo.py (publish/close/update_variants) and
src/api/router.py. Every UPDATE past initial draft creation (generate
variants, publish, close) violates the constraint and fails with
psycopg2.errors.CheckViolation.

Revision ID: 005_fix_exam_status_check
Revises: 004_exam_reports
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "005_fix_exam_status_check"
down_revision = "004_exam_reports"
branch_labels = None
depends_on = None

_OLD_STATUS = ("draft", "published", "archived")
_NEW_STATUS = ("draft", "variants_ready", "active", "closed")


def upgrade() -> None:
    op.drop_constraint("ck_generated_exams_status", "generated_exams", type_="check")
    op.create_check_constraint(
        "ck_generated_exams_status",
        "generated_exams",
        sa.text(f"status IN {_NEW_STATUS!r}"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_generated_exams_status", "generated_exams", type_="check")
    op.create_check_constraint(
        "ck_generated_exams_status",
        "generated_exams",
        sa.text(f"status IN {_OLD_STATUS!r}"),
    )
