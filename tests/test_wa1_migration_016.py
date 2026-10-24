"""Migration 016 (``exam_deletions``) is additive and reversible - checked on a real, EMPTY throwaway Postgres.

    docker run -d --name exam-wa1-pg -e POSTGRES_USER=exam -e POSTGRES_PASSWORD=exam -e POSTGRES_DB=exam_wa1_m \
        -p 127.0.0.1:55442:5432 postgres:15-alpine
    EXAM_WA1_MIGRATION_URL=postgresql://exam:exam@127.0.0.1:55442/exam_wa1_m pytest tests/test_wa1_migration_016.py -q

The database is dropped back to nothing afterwards.  A seeded exam must survive upgrade -> downgrade -> upgrade untouched.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

URL = os.getenv("EXAM_WA1_MIGRATION_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set EXAM_WA1_MIGRATION_URL (a throwaway Postgres) to run")
ROOT = Path(__file__).resolve().parents[1]


def _config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def test_016_upgrades_downgrades_and_upgrades_again_without_touching_exams(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    engine = sa.create_engine(URL)
    config = _config()
    try:
        command.upgrade(config, "015_repair_derivative_distractor")
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO generated_exams (id, title, variants, settings, org_id, status) "
                "VALUES ('EXAM_MIG', 'seed', '[]', '{}', 'ORG1', 'archived')"
            ))
            assert "exam_deletions" not in sa.inspect(conn).get_table_names()

        command.upgrade(config, "head")
        inspector = sa.inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("exam_deletions")}
        assert set(columns) == {"id", "exam_id", "org_id", "title", "deleted_by", "deleted_at", "reason"}
        assert inspector.get_foreign_keys("exam_deletions") == []  # must outlive the exam
        assert {i["name"] for i in inspector.get_indexes("exam_deletions")} >= {"idx_exam_deletions_org_time", "idx_exam_deletions_exam"}

        command.downgrade(config, "-1")
        assert "exam_deletions" not in sa.inspect(engine).get_table_names()

        command.upgrade(config, "head")
        assert "exam_deletions" in sa.inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT title FROM generated_exams WHERE id = 'EXAM_MIG'")).scalar() == "seed"
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            conn.execute(sa.text("CREATE SCHEMA public"))
        engine.dispose()
