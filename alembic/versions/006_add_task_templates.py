"""Add task_templates table backing TaskTemplateDB / TemplateCache

src/domain/template_cache.py reads and writes this table to cache
AI-templatized task variants (so VariantOrchestrator doesn't have to
re-call Gemini for a task it has already templatized). The ORM model
(src/models/shared.py::TaskTemplateDB) has existed since early on, but no
migration ever created the table — generate-variants failed with
psycopg2.errors.UndefinedTable at runtime.

original_task_id / skill_id intentionally have no DB-level FOREIGN KEY:
tasks_master and knowledge_hierarchy live in content-service's own
database (algo_content), not here — exam-service only talks to
content-service over HTTP (see src/clients/content_client.py). These
columns are plain string references resolved at the application layer.

Revision ID: 006_add_task_templates
Revises: 005_fix_exam_status_check
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "006_add_task_templates"
down_revision = "005_fix_exam_status_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.has_table(bind, "task_templates"):
        return

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
    op.create_index(
        "ix_task_templates_original_task_id", "task_templates", ["original_task_id"]
    )
    op.create_index(
        "ix_task_templates_active_quality",
        "task_templates",
        ["original_task_id", "is_active", "quality_score"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_templates_active_quality", table_name="task_templates")
    op.drop_index("ix_task_templates_original_task_id", table_name="task_templates")
    op.drop_table("task_templates")
