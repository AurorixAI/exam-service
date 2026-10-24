"""Keep a trace of permanently deleted exams.

Revision ID: 016_exam_deletions
Revises: 015_repair_derivative_distractor

``DELETE /exams/{id}?permanent=true`` removes the exam, its sessions and (by foreign-key cascade) its lifecycle
history.  This migration adds the table that survives that: one row per permanent deletion (exam id, organisation,
title, who, when, why).  It has NO foreign key to ``generated_exams`` on purpose - it must outlive the exam.

Additive and reversible: ``downgrade`` drops only this table; no exam data is touched either way.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "016_exam_deletions"
down_revision = "015_repair_derivative_distractor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "exam_deletions" not in inspector.get_table_names():
        op.create_table(
            "exam_deletions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("exam_id", sa.String(length=30), nullable=False),
            sa.Column("org_id", sa.String(length=36), nullable=True),
            sa.Column("title", sa.String(length=300), nullable=True),
            sa.Column("deleted_by", sa.String(length=100), nullable=True),
            sa.Column("deleted_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
            sa.Column("reason", sa.String(length=500), nullable=True),
        )
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("exam_deletions")}
    if "idx_exam_deletions_org_time" not in indexes:
        op.create_index("idx_exam_deletions_org_time", "exam_deletions", ["org_id", "deleted_at"])
    if "idx_exam_deletions_exam" not in indexes:
        op.create_index("idx_exam_deletions_exam", "exam_deletions", ["exam_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "exam_deletions" in inspector.get_table_names():
        op.drop_table("exam_deletions")
