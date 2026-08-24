"""Add group and student binding columns to generated_exams and exam_sessions

Revision ID: 007_add_group_and_student_binding
Revises: 006_add_task_templates
Create Date: 2026-08-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "007_group_binding"
down_revision = "006_add_task_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Columns for generated_exams
    columns_exams = [c["name"] for c in sa.inspect(conn).get_columns("generated_exams")]
    if "group_id" not in columns_exams:
        op.add_column("generated_exams", sa.Column("group_id", sa.String(36), nullable=True))
    if "group_name" not in columns_exams:
        op.add_column("generated_exams", sa.Column("group_name", sa.String(200), nullable=True))
    if "class_level" not in columns_exams:
        op.add_column("generated_exams", sa.Column("class_level", sa.Integer(), nullable=True))

    indexes_exams = [ix["name"] for ix in sa.inspect(conn).get_indexes("generated_exams")]
    if "idx_generated_exams_group_id" not in indexes_exams:
        op.create_index("idx_generated_exams_group_id", "generated_exams", ["group_id"])

    # Columns for exam_sessions
    columns_sessions = [c["name"] for c in sa.inspect(conn).get_columns("exam_sessions")]
    if "student_id" not in columns_sessions:
        op.add_column("exam_sessions", sa.Column("student_id", sa.String(36), nullable=True))
    if "display_id" not in columns_sessions:
        op.add_column("exam_sessions", sa.Column("display_id", sa.String(50), nullable=True))

    indexes_sessions = [ix["name"] for ix in sa.inspect(conn).get_indexes("exam_sessions")]
    if "idx_exam_sessions_student_id" not in indexes_sessions:
        op.create_index("idx_exam_sessions_student_id", "exam_sessions", ["student_id"])
    if "idx_exam_sessions_display_id" not in indexes_sessions:
        op.create_index("idx_exam_sessions_display_id", "exam_sessions", ["display_id"])


def downgrade() -> None:
    conn = op.get_bind()

    indexes_sessions = [ix["name"] for ix in sa.inspect(conn).get_indexes("exam_sessions")]
    if "idx_exam_sessions_display_id" in indexes_sessions:
        op.drop_index("idx_exam_sessions_display_id", table_name="exam_sessions")
    if "idx_exam_sessions_student_id" in indexes_sessions:
        op.drop_index("idx_exam_sessions_student_id", table_name="exam_sessions")

    columns_sessions = [c["name"] for c in sa.inspect(conn).get_columns("exam_sessions")]
    if "display_id" in columns_sessions:
        op.drop_column("exam_sessions", "display_id")
    if "student_id" in columns_sessions:
        op.drop_column("exam_sessions", "student_id")

    indexes_exams = [ix["name"] for ix in sa.inspect(conn).get_indexes("generated_exams")]
    if "idx_generated_exams_group_id" in indexes_exams:
        op.drop_index("idx_generated_exams_group_id", table_name="generated_exams")

    columns_exams = [c["name"] for c in sa.inspect(conn).get_columns("generated_exams")]
    if "class_level" in columns_exams:
        op.drop_column("generated_exams", "class_level")
    if "group_name" in columns_exams:
        op.drop_column("generated_exams", "group_name")
    if "group_id" in columns_exams:
        op.drop_column("generated_exams", "group_id")
