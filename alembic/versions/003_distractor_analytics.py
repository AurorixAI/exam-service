"""Add distractor_analytics to exam_sessions

Хранит попадания ученика в дистракторы при сдаче экзамена:
  {task_id: {hit: bool, distractor_value: str, error_type: str, explanation: str}}

Revision ID: 003_distractor_analytics
Revises: 002_add_checks_and_org
Create Date: 2026-06-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "003_distractor_analytics"
down_revision = "002_add_checks_and_org"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "exam_sessions",
        sa.Column("distractor_analytics", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("exam_sessions", "distractor_analytics")
