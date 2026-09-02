"""Add the immutable offline-print delivery mode for exams.

Revision ID: 014_add_print_exam_packages
Revises: 013_exam_lifecycle_history

Printed materials are a protected snapshot, not a browser rendering of a
mutable draft.  This migration only adds new metadata; it never rewrites
existing exam tasks, answers, sessions or reports.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "014_add_print_exam_packages"
down_revision = "013_exam_lifecycle_history"
branch_labels = None
depends_on = None


_EXAM_STATUSES = "('draft', 'variants_ready', 'lobby', 'print_ready', 'active', 'closed', 'archived')"
_DELIVERY_MODES = "('online', 'print', 'hybrid')"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("generated_exams")}
    checks = {item["name"] for item in inspector.get_check_constraints("generated_exams") if item.get("name")}

    if "delivery_mode" not in columns:
        op.add_column(
            "generated_exams",
            sa.Column(
                "delivery_mode",
                sa.String(length=20),
                nullable=False,
                server_default="online",
            ),
        )
    if bind.dialect.name == "postgresql":
        if "ck_generated_exams_status" in checks:
            op.drop_constraint("ck_generated_exams_status", "generated_exams", type_="check")
        op.create_check_constraint(
            "ck_generated_exams_status",
            "generated_exams",
            sa.text(f"status IN {_EXAM_STATUSES}"),
        )
        if "ck_generated_exams_delivery_mode" not in checks:
            op.create_check_constraint(
                "ck_generated_exams_delivery_mode",
                "generated_exams",
                sa.text(f"delivery_mode IN {_DELIVERY_MODES}"),
            )

    if "exam_print_packages" not in inspector.get_table_names():
        op.create_table(
            "exam_print_packages",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "exam_id",
                sa.String(length=30),
                sa.ForeignKey("generated_exams.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("org_id", sa.String(length=36), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="ready"),
            sa.Column("snapshot", postgresql.JSONB(), nullable=False),
            sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
            sa.Column("created_by", sa.String(length=100), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
            sa.Column("released_at", sa.DateTime(), nullable=True),
        )
    package_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("exam_print_packages")}
    if "idx_exam_print_packages_org_created" not in package_indexes:
        op.create_index("idx_exam_print_packages_org_created", "exam_print_packages", ["org_id", "created_at"])

    # A prepared print run occupies the same concrete lesson slot as a live
    # lobby.  Rebuild the partial index to protect both modes at the database
    # level, including concurrent requests.
    op.execute("DROP INDEX IF EXISTS uq_generated_exams_open_lesson")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_generated_exams_open_lesson "
        "ON generated_exams (org_id, scheduled_lesson_id, scheduled_lesson_date) "
        "WHERE status IN ('lobby', 'print_ready', 'active') "
        "AND scheduled_lesson_id IS NOT NULL "
        "AND scheduled_lesson_date IS NOT NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    op.execute("DROP INDEX IF EXISTS uq_generated_exams_open_lesson")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_generated_exams_open_lesson "
        "ON generated_exams (org_id, scheduled_lesson_id, scheduled_lesson_date) "
        "WHERE status IN ('lobby', 'active') "
        "AND scheduled_lesson_id IS NOT NULL "
        "AND scheduled_lesson_date IS NOT NULL"
    )
    if "exam_print_packages" in inspector.get_table_names():
        op.drop_table("exam_print_packages")
    if bind.dialect.name == "postgresql":
        checks = {item["name"] for item in sa.inspect(bind).get_check_constraints("generated_exams") if item.get("name")}
        if "ck_generated_exams_delivery_mode" in checks:
            op.drop_constraint("ck_generated_exams_delivery_mode", "generated_exams", type_="check")
        if "ck_generated_exams_status" in checks:
            op.drop_constraint("ck_generated_exams_status", "generated_exams", type_="check")
        op.create_check_constraint(
            "ck_generated_exams_status",
            "generated_exams",
            sa.text("status IN ('draft', 'variants_ready', 'lobby', 'active', 'closed', 'archived')"),
        )
    columns = {column["name"] for column in sa.inspect(bind).get_columns("generated_exams")}
    if "delivery_mode" in columns:
        op.drop_column("generated_exams", "delivery_mode")
