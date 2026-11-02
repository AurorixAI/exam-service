"""Drop ``task_templates`` - the cache table of the removed exam-variant "template" mode.

Revision ID: 018_drop_task_templates
Revises: 017_exam_pass_threshold

Owner decision 02.11 (wave D): the variant "template" mode (LLM templatizer + SymPy solver + ``template_cache``) is deleted from the
code; this table was read and written only by that code.  Run it only when ``SELECT count(*) FROM task_templates`` is 0 on the target
database (``wave_d_checks.sh``): the rows are a regenerable cache, but nothing here checks that for you.

Revision 006 is NOT changed (``007.down_revision`` points at it).  ``upgrade`` is idempotent (a database that never had the table is fine);
``downgrade`` creates the table again with the schema of 006 - empty, there is nothing to restore the rows from.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "018_drop_task_templates"
down_revision = "017_exam_pass_threshold"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not bind.dialect.has_table(bind, "task_templates"):
        return
    op.drop_index("ix_task_templates_active_quality", table_name="task_templates", if_exists=True)
    op.drop_index("ix_task_templates_original_task_id", table_name="task_templates", if_exists=True)
    op.drop_table("task_templates")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.has_table(bind, "task_templates"):
        return
    # The schema of revision 006_add_task_templates, unchanged.
    op.create_table(
        "task_templates",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("original_task_id", sa.String(30), nullable=False),
        sa.Column("skill_id", sa.String(30), nullable=True),
        sa.Column("template_text", sa.Text(), nullable=True),
        sa.Column("template_latex", sa.Text(), nullable=True),
        sa.Column("parameters", postgresql.JSONB(), nullable=True),
        sa.Column("constraints", postgresql.JSONB(), nullable=True),
        sa.Column("answer_formula", sa.Text(), nullable=True),
        sa.Column("task_type", sa.String(30), nullable=True),
        sa.Column("text_template", sa.Text(), nullable=True),
        sa.Column("original_values", postgresql.JSONB(), nullable=True),
        sa.Column("param_details", postgresql.JSONB(), nullable=True),
        sa.Column("answer_formula_sympy", sa.Text(), nullable=True),
        sa.Column("constraints_repr", sa.Text(), nullable=True),
        sa.Column("generation_method", sa.String(20), server_default="llm"),
        sa.Column("quality_score", sa.Float(), server_default="0.0"),
        sa.Column("tested_combinations", sa.Integer(), server_default="0"),
        sa.Column("successful_combinations", sa.Integer(), server_default="0"),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_index("ix_task_templates_original_task_id", "task_templates", ["original_task_id"])
    op.create_index(
        "ix_task_templates_active_quality",
        "task_templates",
        ["original_task_id", "is_active", "quality_score"],
    )
