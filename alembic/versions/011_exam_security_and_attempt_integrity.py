"""Secure exam tenancy and student attempt capabilities.

Revision ID: 011_exam_security
Revises: 010_archived_status
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "011_exam_security"
down_revision = "010_archived_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    exam_columns = {column["name"] for column in inspector.get_columns("generated_exams")}
    session_columns = {column["name"] for column in inspector.get_columns("exam_sessions")}
    indexes = {index["name"] for index in inspector.get_indexes("exam_sessions")}

    if "org_id" not in exam_columns:
        op.add_column("generated_exams", sa.Column("org_id", sa.String(length=36), nullable=True))
    exam_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("generated_exams")}
    # Revision 002 already used ``idx_ge_org_id`` in some environments.
    # Do not create an unnecessary second index over the same column.
    if not {"idx_generated_exams_org_id", "idx_ge_org_id"} & exam_indexes:
        op.create_index("idx_generated_exams_org_id", "generated_exams", ["org_id"])

    if "session_token" not in session_columns:
        op.add_column("exam_sessions", sa.Column("session_token", sa.String(length=128), nullable=True))
    if "uq_exam_sessions_session_token" not in indexes:
        op.create_unique_constraint("uq_exam_sessions_session_token", "exam_sessions", ["session_token"])

    # One authenticated roster member can have only one attempt per exam.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_exam_sessions_exam_student "
        "ON exam_sessions (exam_id, student_id) WHERE student_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_exam_sessions_exam_student")
    inspector = sa.inspect(op.get_bind())
    session_columns = {column["name"] for column in inspector.get_columns("exam_sessions")}
    constraints = {item["name"] for item in inspector.get_unique_constraints("exam_sessions")}
    indexes = {item["name"] for item in inspector.get_indexes("generated_exams")}
    if "uq_exam_sessions_session_token" in constraints:
        op.drop_constraint("uq_exam_sessions_session_token", "exam_sessions", type_="unique")
    if "session_token" in session_columns:
        op.drop_column("exam_sessions", "session_token")
    if "idx_generated_exams_org_id" in indexes:
        op.drop_index("idx_generated_exams_org_id", table_name="generated_exams")
    exam_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("generated_exams")}
    if "org_id" in exam_columns:
        op.drop_column("generated_exams", "org_id")
