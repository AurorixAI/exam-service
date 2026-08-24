"""Bind published exams to one concrete scheduled lesson occurrence.

Revision ID: 012_exam_lesson_binding
Revises: 011_exam_security
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "012_exam_lesson_binding"
down_revision = "011_exam_security"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("generated_exams")}
    indexes = {index["name"] for index in inspector.get_indexes("generated_exams")}

    if "scheduled_lesson_id" not in columns:
        op.add_column(
            "generated_exams",
            sa.Column("scheduled_lesson_id", sa.String(length=100), nullable=True),
        )
    if "scheduled_lesson_date" not in columns:
        op.add_column(
            "generated_exams",
            sa.Column("scheduled_lesson_date", sa.String(length=10), nullable=True),
        )

    if "idx_generated_exams_lesson_binding" not in indexes:
        op.create_index(
            "idx_generated_exams_lesson_binding",
            "generated_exams",
            ["org_id", "scheduled_lesson_id", "scheduled_lesson_date"],
        )

    # One lesson can have only one public, open exam.  Historical rows are not
    # rewritten here; the partial index only protects new or explicitly moved
    # lobby/active exams that have a concrete lesson binding.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_generated_exams_open_lesson "
        "ON generated_exams (org_id, scheduled_lesson_id, scheduled_lesson_date) "
        "WHERE status IN ('lobby', 'active') "
        "AND scheduled_lesson_id IS NOT NULL "
        "AND scheduled_lesson_date IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_generated_exams_open_lesson")
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("generated_exams")}
    columns = {column["name"] for column in inspector.get_columns("generated_exams")}
    if "idx_generated_exams_lesson_binding" in indexes:
        op.drop_index("idx_generated_exams_lesson_binding", table_name="generated_exams")
    if "scheduled_lesson_date" in columns:
        op.drop_column("generated_exams", "scheduled_lesson_date")
    if "scheduled_lesson_id" in columns:
        op.drop_column("generated_exams", "scheduled_lesson_id")
