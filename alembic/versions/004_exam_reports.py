"""Add exam_reports table for on-demand LLM narrative reports

Each exam gets at most one report row (unique constraint on exam_id).
Generated on teacher request via POST /exams/{exam_id}/report.

Revision ID: 004_exam_reports
Revises: 003_distractor_analytics
Create Date: 2026-06-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "004_exam_reports"
down_revision = "003_distractor_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS — safe to re-run if table was created outside migration
    bind = op.get_bind()
    if bind.dialect.has_table(bind, "exam_reports"):
        return

    op.create_table(
        "exam_reports",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column(
            "exam_id",
            sa.String(30),
            sa.ForeignKey("generated_exams.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("total_students", sa.Integer(), default=0),
        sa.Column("submitted_count", sa.Integer(), default=0),
        sa.Column("avg_score_pct", sa.Numeric(5, 2), default=0),
        sa.Column("pass_rate_pct", sa.Numeric(5, 2), default=0),
        sa.Column("task_analytics", sa.JSON(), nullable=True),
        sa.Column("top_errors", sa.JSON(), nullable=True),
        sa.Column("narrative_ru", sa.Text(), nullable=True),
        sa.Column("narrative_generated", sa.Boolean(), default=False),
        sa.Column("is_final", sa.Boolean(), default=False),
        sa.Column(
            "generated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
        ),
    )
    op.create_index("ix_exam_reports_exam_id", "exam_reports", ["exam_id"])


def downgrade() -> None:
    op.drop_index("ix_exam_reports_exam_id", table_name="exam_reports")
    op.drop_table("exam_reports")
