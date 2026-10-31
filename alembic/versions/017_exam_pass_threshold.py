"""Every exam has its own pass threshold (``generated_exams.pass_threshold_pct``).

Revision ID: 017_exam_pass_threshold
Revises: 016_exam_deletions

Owner decision 31.10: the threshold of "the student passed" belongs to the exam (1..100 %, 60 by default); every
existing exam gets 60.  ``DEFAULT 60`` is a constant, so PostgreSQL adds the column without rewriting the table.  The CHECK goes in
``NOT VALID`` and is validated afterwards, but Alembic runs the whole migration in ONE transaction: ``ADD CONSTRAINT`` holds
``ACCESS EXCLUSIVE`` on ``generated_exams`` until the commit, so ``VALIDATE`` scans the table under that lock.  That is acceptable
here (the table holds one row per exam - hundreds, not millions - and the scan takes milliseconds); a table that grows large
would need ``VALIDATE`` in its own transaction (``autocommit_block``).

Additive and reversible: ``downgrade`` drops the constraint and the column only.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "017_exam_pass_threshold"
down_revision = "016_exam_deletions"
branch_labels = None
depends_on = None

_CHECK = "ck_generated_exams_pass_threshold_pct"


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("generated_exams")}
    if "pass_threshold_pct" not in columns:
        op.add_column(
            "generated_exams",
            sa.Column("pass_threshold_pct", sa.Integer(), nullable=False, server_default=sa.text("60")),
        )
    if bind.dialect.name == "postgresql":
        exists = bind.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": _CHECK}).scalar()
        if not exists:
            op.execute(
                f"ALTER TABLE generated_exams ADD CONSTRAINT {_CHECK} "
                "CHECK (pass_threshold_pct BETWEEN 1 AND 100) NOT VALID"
            )
        op.execute(f"ALTER TABLE generated_exams VALIDATE CONSTRAINT {_CHECK}")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"ALTER TABLE generated_exams DROP CONSTRAINT IF EXISTS {_CHECK}")
    columns = {c["name"] for c in sa.inspect(bind).get_columns("generated_exams")}
    if "pass_threshold_pct" in columns:
        op.drop_column("generated_exams", "pass_threshold_pct")
