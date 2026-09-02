"""Add is_pinned and source_exam_id to generated_exams

Revision ID: 008_pinned_and_clone
Revises: 007_group_binding
Create Date: 2026-08-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "008_pinned_and_clone"
down_revision = "007_group_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    columns = [c["name"] for c in sa.inspect(conn).get_columns("generated_exams")]

    if "is_pinned" not in columns:
        op.add_column(
            "generated_exams",
            sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default="false"),
        )

    if "source_exam_id" not in columns:
        op.add_column(
            "generated_exams",
            sa.Column("source_exam_id", sa.String(30), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    columns = [c["name"] for c in sa.inspect(conn).get_columns("generated_exams")]

    if "source_exam_id" in columns:
        op.drop_column("generated_exams", "source_exam_id")

    if "is_pinned" in columns:
        op.drop_column("generated_exams", "is_pinned")
