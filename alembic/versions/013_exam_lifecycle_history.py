"""Add immutable operational history for administered exams.

Revision ID: 013_exam_lifecycle_history
Revises: 012_exam_lesson_binding
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "013_exam_lifecycle_history"
down_revision = "012_exam_lesson_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "exam_lifecycle_events" not in inspector.get_table_names():
        op.create_table(
            "exam_lifecycle_events",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("exam_id", sa.String(length=30), sa.ForeignKey("generated_exams.id", ondelete="CASCADE"), nullable=False),
            sa.Column("org_id", sa.String(length=36), nullable=False),
            sa.Column("event_type", sa.String(length=50), nullable=False),
            sa.Column("occurred_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
            sa.Column("actor_id", sa.String(length=100), nullable=True),
            sa.Column("group_id", sa.String(length=36), nullable=True),
            sa.Column("group_name", sa.String(length=200), nullable=True),
            sa.Column("lesson_id", sa.String(length=100), nullable=True),
            sa.Column("lesson_date", sa.String(length=10), nullable=True),
            sa.Column("previous_lesson_id", sa.String(length=100), nullable=True),
            sa.Column("previous_lesson_date", sa.String(length=10), nullable=True),
            sa.Column("reason", sa.String(length=500), nullable=True),
            sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        )
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("exam_lifecycle_events")}
    if "idx_exam_lifecycle_org_time" not in indexes:
        op.create_index("idx_exam_lifecycle_org_time", "exam_lifecycle_events", ["org_id", "occurred_at"])
    if "idx_exam_lifecycle_exam_time" not in indexes:
        op.create_index("idx_exam_lifecycle_exam_time", "exam_lifecycle_events", ["exam_id", "occurred_at"])
    if "ix_exam_lifecycle_events_event_type" not in indexes:
        op.create_index("ix_exam_lifecycle_events_event_type", "exam_lifecycle_events", ["event_type"])

    # The previous release kept a bounded history in generated_exams.settings.
    # Preserve it once so the new administrator monitor does not start with a
    # blank history after deployment.  New events are written only to this
    # table.  The guard makes the migration safe to rerun on interrupted local
    # deployments without duplicating an exam's timeline.
    if bind.dialect.name != "postgresql":
        return

    legacy_rows = bind.execute(sa.text("""
        SELECT id, org_id, group_id, group_name, settings
        FROM generated_exams
        WHERE settings IS NOT NULL
          AND settings ? 'lifecycle_events'
    """)).mappings()
    existing_exam_ids = {
        row["exam_id"]
        for row in bind.execute(
            sa.text("SELECT DISTINCT exam_id FROM exam_lifecycle_events")
        ).mappings()
    }
    for exam in legacy_rows:
        if exam["id"] in existing_exam_ids:
            continue
        settings = exam["settings"] if isinstance(exam["settings"], dict) else {}
        events = settings.get("lifecycle_events") if isinstance(settings, dict) else None
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict) or not event.get("type"):
                continue
            occurred_at = datetime.utcnow()
            raw_timestamp = event.get("at")
            if isinstance(raw_timestamp, str):
                try:
                    occurred_at = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    pass
            bind.execute(sa.text("""
                INSERT INTO exam_lifecycle_events (
                    id, exam_id, org_id, event_type, occurred_at, group_id,
                    group_name, lesson_id, lesson_date, previous_lesson_id,
                    previous_lesson_date, reason, details
                ) VALUES (
                    :id, :exam_id, :org_id, :event_type, :occurred_at, :group_id,
                    :group_name, :lesson_id, :lesson_date, :previous_lesson_id,
                    :previous_lesson_date, :reason, CAST(:details AS jsonb)
                )
            """), {
                "id": uuid4().hex,
                "exam_id": exam["id"],
                "org_id": event.get("org_id") or exam["org_id"],
                "event_type": str(event["type"]),
                "occurred_at": occurred_at,
                "group_id": event.get("group_id") or exam["group_id"],
                "group_name": event.get("group_name") or exam["group_name"],
                "lesson_id": event.get("lesson_id"),
                "lesson_date": event.get("lesson_date"),
                "previous_lesson_id": event.get("previous_lesson_id"),
                "previous_lesson_date": event.get("previous_lesson_date"),
                "reason": event.get("reason"),
                "details": sa.JSON().bind_processor(bind.dialect)(event.get("details") if isinstance(event.get("details"), dict) else {}),
            })


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "exam_lifecycle_events" in inspector.get_table_names():
        op.drop_table("exam_lifecycle_events")
