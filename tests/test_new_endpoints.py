"""
Tests for newly added endpoints:
  - POST /exams/{id}/close
  - GET  /exams/{id}/sessions
  - GET  /exams/{id}/analytics
  - POST /exams/{id}/generate-variants
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("APP_ENV", "testing")

import pytest
from unittest.mock import patch, MagicMock
from tests.conftest import make_task, make_exam_in_db, make_session_in_db
from src.models.exam_models import ExamSession
from datetime import datetime, timezone


def _submit_session(client, session_id, answers: dict):
    return client.post(f"/api/v1/exam-sessions/{session_id}/submit", json={
        "answers": answers,
        "time_spent_seconds": 120,
    })


# ═══════════════════════════════════════════════════════════════════════════
# Close exam
# ═══════════════════════════════════════════════════════════════════════════

class TestCloseExam:
    def test_close_active_exam(self, client, db):
        exam = make_exam_in_db(db, status="active")
        resp = client.post(f"/api/v1/exams/{exam.id}/close")
        assert resp.status_code == 200
        assert resp.json()["status"] == "closed"

    def test_close_draft_exam(self, client, db):
        exam = make_exam_in_db(db, status="draft")
        resp = client.post(f"/api/v1/exams/{exam.id}/close")
        assert resp.status_code == 200
        assert resp.json()["status"] == "closed"

    def test_close_nonexistent(self, client):
        resp = client.post("/api/v1/exams/NOPE/close")
        assert resp.status_code == 404

    def test_closed_exam_rejects_new_starts(self, client, db):
        exam = make_exam_in_db(db, status="active")
        exam.access_code = "EXCLS0001"
        db.commit()

        client.post(f"/api/v1/exams/{exam.id}/close")
        resp = client.post("/api/v1/exams/by-code/EXCLS0001/start", json={
            "student_name": "Студент",
        })
        assert resp.status_code == 410


# ═══════════════════════════════════════════════════════════════════════════
# List sessions
# ═══════════════════════════════════════════════════════════════════════════

class TestListSessions:
    def test_empty_sessions(self, client, db):
        exam = make_exam_in_db(db)
        resp = client.get(f"/api/v1/exams/{exam.id}/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_multiple_sessions(self, client, db):
        tasks = [make_task("T1", answer="5"), make_task("T2", answer="10")]
        exam = make_exam_in_db(db, tasks=tasks)

        # Create two sessions manually with unique IDs
        for i in range(3):
            sess = ExamSession(
                id=f"SES_LIST_{i:02d}",
                exam_id=exam.id,
                student_name=f"Студент {i}",
                variant_number=1,
                answers={},
                max_score=20,
                status="in_progress",
            )
            db.add(sess)
        db.commit()

        resp = client.get(f"/api/v1/exams/{exam.id}/sessions")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_sessions_show_correct_fields(self, client, db):
        exam = make_exam_in_db(db)
        session = make_session_in_db(db, exam, status="in_progress")

        resp = client.get(f"/api/v1/exams/{exam.id}/sessions")
        s = resp.json()[0]
        assert s["session_id"] == session.id
        assert s["student_name"] == "Иван Иванов"
        assert "score" in s
        assert "percentage" in s

    def test_submitted_session_shown(self, client, db):
        tasks = [make_task("T1", answer="5")]
        exam = make_exam_in_db(db, tasks=tasks)
        session = make_session_in_db(db, exam)

        _submit_session(client, session.id, {"T1": "5"})

        resp = client.get(f"/api/v1/exams/{exam.id}/sessions")
        assert resp.json()[0]["status"] == "submitted"
        assert resp.json()[0]["correct_count"] == 1

    def test_sessions_nonexistent_exam(self, client):
        resp = client.get("/api/v1/exams/NOEXIST/sessions")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# Analytics
# ═══════════════════════════════════════════════════════════════════════════

class TestAnalytics:
    def test_analytics_no_sessions(self, client, db):
        exam = make_exam_in_db(db)
        resp = client.get(f"/api/v1/exams/{exam.id}/analytics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_sessions"] == 0
        assert data["submitted_sessions"] == 0
        assert data["avg_score_pct"] == 0.0

    def test_analytics_with_submissions(self, client, db):
        tasks = [make_task("T1", answer="5"), make_task("T2", answer="10")]
        exam = make_exam_in_db(db, tasks=tasks)

        # Session 1: all correct (100%)
        s1 = ExamSession(id="SES_ANA_01", exam_id=exam.id,
                         student_name="Студент 1", variant_number=1,
                         answers={}, max_score=20, status="in_progress")
        # Session 2: all wrong (0%)
        s2 = ExamSession(id="SES_ANA_02", exam_id=exam.id,
                         student_name="Студент 2", variant_number=1,
                         answers={}, max_score=20, status="in_progress")
        db.add_all([s1, s2])
        db.commit()

        _submit_session(client, "SES_ANA_01", {"T1": "5", "T2": "10"})
        _submit_session(client, "SES_ANA_02", {"T1": "99", "T2": "99"})

        resp = client.get(f"/api/v1/exams/{exam.id}/analytics")
        data = resp.json()
        assert data["submitted_sessions"] == 2
        assert data["avg_score_pct"] == pytest.approx(50.0, abs=0.1)

    def test_analytics_pass_rate(self, client, db):
        tasks = [make_task("T1", answer="5")]
        exam = make_exam_in_db(db, tasks=tasks)

        # 3 sessions: 2 pass (100%), 1 fail (0%)
        for i, ans in enumerate(["5", "5", "99"]):
            s = ExamSession(id=f"SES_PR_{i:02d}", exam_id=exam.id,
                            student_name=f"С{i}", variant_number=1,
                            answers={}, max_score=10, status="in_progress")
            db.add(s)
        db.commit()

        for i, ans in enumerate(["5", "5", "99"]):
            _submit_session(client, f"SES_PR_{i:02d}", {"T1": ans})

        resp = client.get(f"/api/v1/exams/{exam.id}/analytics")
        data = resp.json()
        assert data["pass_rate_pct"] == pytest.approx(66.7, abs=0.1)

    def test_analytics_skill_gaps_sorted(self, client, db):
        """Tasks with worst performance come first in skill_gaps."""
        tasks = [
            make_task("T1", answer="5"),
            make_task("T2", answer="10"),
        ]
        exam = make_exam_in_db(db, tasks=tasks)

        s1 = ExamSession(id="SES_GAP_01", exam_id=exam.id,
                         student_name="С1", variant_number=1,
                         answers={}, max_score=20, status="in_progress")
        db.add(s1)
        db.commit()

        # T1 correct, T2 wrong
        _submit_session(client, "SES_GAP_01", {"T1": "5", "T2": "99"})

        resp = client.get(f"/api/v1/exams/{exam.id}/analytics")
        gaps = resp.json()["skill_gaps"]
        # T2 has 0% correct rate — should be first
        assert gaps[0]["task_id"] == "T2"
        assert gaps[0]["correct_rate"] == 0.0
        assert gaps[1]["task_id"] == "T1"
        assert gaps[1]["correct_rate"] == 1.0

    def test_analytics_nonexistent_exam(self, client):
        resp = client.get("/api/v1/exams/NOEXIST/analytics")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# Generate variants
# ═══════════════════════════════════════════════════════════════════════════

class TestGenerateVariants:
    def test_generate_variants_calls_orchestrator(self, client, db):
        tasks = [make_task(f"T{i}", answer=str(i)) for i in range(3)]
        exam = make_exam_in_db(db, tasks=tasks, status="draft")

        # Mock the orchestrator
        mock_report = MagicMock()
        mock_report.variants = [MagicMock(to_dict=lambda: {"variant_number": i+1, "tasks": tasks}) for i in range(3)]
        mock_report.total_variants = 3
        mock_report.templatized_count = 2
        mock_report.original_count = 1

        with patch("src.api.router.VariantOrchestrator") as MockOrch:
            MockOrch.return_value.generate_variants.return_value = mock_report
            resp = client.post(f"/api/v1/exams/{exam.id}/generate-variants?variants_count=3")

        assert resp.status_code == 200
        data = resp.json()
        assert data["variants_count"] == 3
        assert data["status"] == "variants_ready"

    def test_generate_variants_nonexistent_exam(self, client):
        resp = client.post("/api/v1/exams/NOEXIST/generate-variants?variants_count=3")
        assert resp.status_code == 404

    def test_generate_variants_for_active_exam_fails(self, client, db):
        exam = make_exam_in_db(db, status="active")
        resp = client.post(f"/api/v1/exams/{exam.id}/generate-variants?variants_count=3")
        assert resp.status_code == 400
