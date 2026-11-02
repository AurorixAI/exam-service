"""Migration 018 (drop ``task_templates``) on a real, EMPTY throwaway PostgreSQL: it drops the table, leaves exams alone, is idempotent,
and its downgrade brings back the exact schema of revision 006.

    docker run -d --name exam-wda-pg -e POSTGRES_USER=exam -e POSTGRES_PASSWORD=exam -e POSTGRES_DB=exam_m18 \
        -p 127.0.0.1:55482:5432 postgres:15-alpine
    EXAM_WDA_MIGRATION_URL=postgresql://exam:exam@127.0.0.1:55482/exam_m18 pytest tests/test_wd_a_migration_018.py -q

The schema is dropped back to nothing afterwards.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

URL = os.getenv("EXAM_WDA_MIGRATION_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set EXAM_WDA_MIGRATION_URL (a throwaway Postgres) to run")
ROOT = Path(__file__).resolve().parents[1]


def _config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _shape(engine) -> dict:
    """Columns (name, type, nullable) and indexes (name, columns) of task_templates, comparable between two moments."""
    inspector = sa.inspect(engine)
    return {
        "columns": [(c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns("task_templates")],
        "indexes": sorted((i["name"], tuple(i["column_names"])) for i in inspector.get_indexes("task_templates")),
        "pk": inspector.get_pk_constraint("task_templates")["constrained_columns"],
    }


def test_018_drops_the_cache_table_and_its_downgrade_restores_revision_006(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    engine = sa.create_engine(URL)
    config = _config()
    try:
        command.upgrade(config, "017_exam_pass_threshold")
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO generated_exams (id, title, variants, settings, org_id, status) "
                "VALUES ('EXAM_MIG18', 'seed', '[]', '{}', 'ORG1', 'draft')"
            ))
        assert "task_templates" in sa.inspect(engine).get_table_names()          # created by 006
        before = _shape(engine)
        assert [name for name, _, _ in before["columns"]][:3] == ["id", "original_task_id", "skill_id"]

        command.upgrade(config, "018_drop_task_templates")
        assert "task_templates" not in sa.inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT title FROM generated_exams WHERE id = 'EXAM_MIG18'")).scalar() == "seed"
            assert conn.execute(sa.text("SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'ix_task_templates%'")).scalar() == 0

        command.downgrade(config, "017_exam_pass_threshold")
        assert _shape(engine) == before                                         # the schema of 006, byte for byte as inspected

        command.upgrade(config, "head")
        command.upgrade(config, "head")                                         # idempotent
        assert "task_templates" not in sa.inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT title FROM generated_exams WHERE id = 'EXAM_MIG18'")).scalar() == "seed"
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            conn.execute(sa.text("CREATE SCHEMA public"))
        engine.dispose()


def test_018_is_a_no_op_on_a_database_that_never_had_the_table(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    engine = sa.create_engine(URL)
    config = _config()
    try:
        command.upgrade(config, "017_exam_pass_threshold")
        with engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE task_templates"))
        command.upgrade(config, "018_drop_task_templates")                      # nothing to drop: must not fail
        assert "task_templates" not in sa.inspect(engine).get_table_names()
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            conn.execute(sa.text("CREATE SCHEMA public"))
        engine.dispose()
