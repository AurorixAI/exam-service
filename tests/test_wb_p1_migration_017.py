"""Migration 017 (``generated_exams.pass_threshold_pct``) and the library query on a real, EMPTY throwaway PostgreSQL.

    docker run -d --name wbp1-exam-pg -e POSTGRES_USER=exam -e POSTGRES_PASSWORD=exam -e POSTGRES_DB=exam_wbp1 \
        -p 127.0.0.1:55461:5432 postgres:15-alpine
    EXAM_WBP1_URL=postgresql://exam:exam@127.0.0.1:55461/exam_wbp1 pytest tests/test_wb_p1_migration_017.py -q

The schema is dropped back to nothing afterwards.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

URL = os.getenv("EXAM_WBP1_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set EXAM_WBP1_URL (a throwaway Postgres) to run")
ROOT = Path(__file__).resolve().parents[1]


def _config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _wipe(engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))


def _insert(conn, exam_id: str, **extra) -> None:
    columns = {"id": exam_id, "title": "seed", "variants": "[]", "settings": "{}", "org_id": "ORG1", "status": "draft", **extra}
    names = ", ".join(columns)
    marks = ", ".join(f":{name}" for name in columns)
    conn.execute(sa.text(f"INSERT INTO generated_exams ({names}) VALUES ({marks})"), columns)


def test_017_gives_every_existing_exam_60_enforces_1_100_and_is_reversible(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    engine = sa.create_engine(URL)
    config = _config()
    try:
        command.upgrade(config, "016_exam_deletions")
        with engine.begin() as conn:
            _insert(conn, "EXAM_OLD1")
            _insert(conn, "EXAM_OLD2", status="archived")
            assert "pass_threshold_pct" not in {c["name"] for c in sa.inspect(conn).get_columns("generated_exams")}

        command.upgrade(config, "head")
        inspector = sa.inspect(engine)
        column = next(c for c in inspector.get_columns("generated_exams") if c["name"] == "pass_threshold_pct")
        assert column["nullable"] is False
        with engine.begin() as conn:
            assert conn.execute(sa.text("SELECT pass_threshold_pct FROM generated_exams ORDER BY id")).scalars().all() == [60, 60]
            _insert(conn, "EXAM_NEW1", pass_threshold_pct=70)
            _insert(conn, "EXAM_NEW2")                                                      # no value -> the default
            assert conn.execute(sa.text("SELECT pass_threshold_pct FROM generated_exams WHERE id='EXAM_NEW2'")).scalar() == 60
            validated = conn.execute(sa.text(
                "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_generated_exams_pass_threshold_pct'")).scalar()
            assert validated is True
        for bad in (0, 101, -5):
            with pytest.raises(sa.exc.IntegrityError):
                with engine.begin() as conn:
                    _insert(conn, f"EXAM_BAD{bad}", pass_threshold_pct=bad)
        for edge in (1, 100):
            with engine.begin() as conn:
                _insert(conn, f"EXAM_EDGE{edge}", pass_threshold_pct=edge)

        command.downgrade(config, "-1")
        inspector = sa.inspect(engine)
        assert "pass_threshold_pct" not in {c["name"] for c in inspector.get_columns("generated_exams")}
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM pg_constraint WHERE conname = 'ck_generated_exams_pass_threshold_pct'")).scalar() == 0
            assert conn.execute(sa.text("SELECT count(*) FROM generated_exams WHERE id IN ('EXAM_OLD1','EXAM_OLD2')")).scalar() == 2   # nothing else touched

        command.upgrade(config, "head")                                                       # and forward again, idempotently
        command.upgrade(config, "head")
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT pass_threshold_pct FROM generated_exams WHERE id='EXAM_OLD1'")).scalar() == 60
    finally:
        _wipe(engine)


def test_the_library_query_runs_on_postgresql(monkeypatch):
    """The visibility clause (GROUP BY + CASE + OR of ANDs), the page order and the count are valid PostgreSQL, not only SQLite."""
    monkeypatch.setenv("DATABASE_URL", URL)
    engine = sa.create_engine(URL)
    command.upgrade(_config(), "head")
    try:
        from datetime import datetime, timedelta

        from fastapi.testclient import TestClient
        from sqlalchemy.orm import sessionmaker

        from src.core import auth as core_auth
        from src.core.auth import ExamActor, require_exam_staff
        from src.core.database import get_db
        from src.main import app

        group = "aaaaaaaa-0000-0000-0000-00000000000a"
        base = datetime(2026, 9, 1, 8, 0, 0)
        with engine.begin() as conn:
            for i in range(120):
                _insert(
                    conn, f"EX{i:04d}", org_id="ORG1", created_by="T1" if i % 2 == 0 else "T2",
                    group_id=group if i % 4 == 0 else None, created_at=base + timedelta(minutes=i),
                )

        async def group_access(actor, group_id):
            return {"id": group_id, "teacher_id": "T1"}

        async def no_substitution(actor, group_id):
            return None

        monkeypatch.setattr(core_auth, "require_group_access", group_access)
        monkeypatch.setattr(core_auth, "substitute_lessons", no_substitution)
        Session = sessionmaker(bind=engine)

        def override_db():
            with Session() as session:
                yield session

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[require_exam_staff] = lambda: ExamActor(user_id="T1", org_id="ORG1", role="teacher", auth_headers={})
        try:
            with TestClient(app) as http:
                ids, page = [], 1
                while True:
                    body = http.get("/api/v1/exams", params={"page": page, "size": 25}).json()
                    ids += [item["exam_id"] for item in body["items"]]
                    if not body["has_more"]:
                        break
                    page += 1
        finally:
            app.dependency_overrides.clear()
        # T1 owns the even rows (60); every fourth row is in the group T1 teaches, the other even rows are his templates;
        # the odd rows of group... none (i % 4 == 0 is even), so 60 in all.
        assert len(ids) == len(set(ids)) == 60
        assert body["total"] == 60
    finally:
        _wipe(engine)
