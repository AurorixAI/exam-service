"""
Integration tests — full HTTP API flow using TestClient + SQLite in-memory.

Covers every endpoint end-to-end:
  1. POST /exams/generate
  2. POST /exams/{id}/publish
  3. GET  /exams/{id}
  4. GET  /exams/by-code/{code}
  5. POST /exams/by-code/{code}/start
  6. POST /exam-sessions/{id}/submit
  7. GET  /exam-sessions/{id}/results
  8. GET  /exams/{id}/sessions (list sessions)
  9. POST /exams/{id}/close
  10. Textbook endpoints (mocked DB rows)
  11. Error paths (404, 400, 410)
  12. Full student journey (generate → publish → start → submit → results)
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("APP_ENV", "testing")

import pytest
from unittest.mock import patch, MagicMock
from datetime import date as _date

from tests.conftest import (
    TEST_GROUP_ID,
    TEST_LESSON_ID,
    make_task,
    make_exam_in_db,
    make_session_in_db,
)
from src.models.exam_models import ExamLifecycleEvent


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

TASKS_POOL = [
    make_task(f"TASK_{i:03d}", answer=str(i * 3), difficulty="B")
    for i in range(1, 11)
]


def mock_task_repo(tasks=None):
    """Returns a context-manager mock that replaces TaskRepository."""
    if tasks is None:
        tasks = TASKS_POOL
    m = MagicMock()
    m.get_tasks_by_skills.return_value = tasks
    m.get_tasks_by_toc_ids.return_value = tasks
    m.expand_toc_ids.return_value = list(range(1, 5))
    return m


# ═══════════════════════════════════════════════════════════════════════════
# 1. POST /api/v1/exams/generate
# ═══════════════════════════════════════════════════════════════════════════

class TestGenerateExam:
    def test_generate_by_skills_success(self, client):
        with patch("src.api.router.TaskRepository", return_value=mock_task_repo()):
            resp = client.post("/api/v1/exams/generate", json={
                "title": "Алгебра 8 класс",
                "skill_ids": ["SK_001", "SK_002"],
                "tasks_count": 5,
                "time_minutes": 45,
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "draft"
        assert data["tasks_count"] == 5
        assert "exam_id" in data

    def test_generate_records_lifecycle_event_after_exam(self, client, db):
        """Creation history must never be flushed before its parent exam."""
        with patch("src.api.router.TaskRepository", return_value=mock_task_repo()):
            response = client.post("/api/v1/exams/generate", json={
                "title": "Экзамен с историей",
                "skill_ids": ["SK_001"],
                "tasks_count": 5,
                "time_minutes": 45,
            })

        assert response.status_code == 200
        exam_id = response.json()["exam_id"]
        event = db.query(ExamLifecycleEvent).filter_by(exam_id=exam_id).one()
        assert event.event_type == "exam_created"

    def test_generate_by_toc_ids_success(self, client):
        with patch("src.api.router.TaskRepository", return_value=mock_task_repo()):
            resp = client.post("/api/v1/exams/generate", json={
                "title": "Учебник Алгебра 8",
                "paragraph_ids": [1, 2, 3],
                "tasks_count": 5,
                "time_minutes": 30,
            })
        assert resp.status_code == 200
        assert resp.json()["tasks_count"] == 5

    def test_generate_fails_without_source(self, client):
        resp = client.post("/api/v1/exams/generate", json={
            "title": "Без источника",
            "tasks_count": 5,
        })
        assert resp.status_code == 400
        assert "skill_ids" in resp.json()["detail"] or "paragraph_ids" in resp.json()["detail"]

    def test_generate_fails_no_tasks_found(self, client):
        empty_repo = MagicMock()
        empty_repo.get_tasks_by_skills.return_value = []
        with patch("src.api.router.TaskRepository", return_value=empty_repo):
            resp = client.post("/api/v1/exams/generate", json={
                "title": "Пустой экзамен",
                "skill_ids": ["SK_NONEXISTENT"],
                "tasks_count": 5,
            })
        assert resp.status_code == 400

    def test_generate_title_too_short(self, client):
        resp = client.post("/api/v1/exams/generate", json={
            "title": "",
            "skill_ids": ["SK_001"],
            "tasks_count": 5,
        })
        assert resp.status_code == 422  # Pydantic validation


# ═══════════════════════════════════════════════════════════════════════════
# 2. POST /api/v1/exams/{id}/publish
# ═══════════════════════════════════════════════════════════════════════════

class TestPublishExam:
    """Publishing binds an exam to one concrete lesson and opens its lobby.

    A published exam does not become ``active`` on its own any more: the
    teacher still has to start the lesson occurrence.
    """

    def _lesson(self, **extra):
        payload = {"lesson_id": TEST_LESSON_ID, "lesson_date": _date.today().isoformat()}
        payload.update(extra)
        return payload

    def test_publish_draft_success(self, client, db):
        exam = make_exam_in_db(db, status="draft")
        resp = client.post(
            f"/api/v1/exams/{exam.id}/publish",
            json=self._lesson(teacher_name="Айгуль Серикова"),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "lobby"
        assert data["access_code"].startswith("EX")
        assert len(data["access_code"]) == 10

    def test_publish_requires_a_lesson_occurrence(self, client, db):
        exam = make_exam_in_db(db, status="draft")
        resp = client.post(f"/api/v1/exams/{exam.id}/publish", json={})
        assert resp.status_code == 422

    def test_publish_not_found(self, client):
        resp = client.post("/api/v1/exams/NONEXISTENT/publish", json=self._lesson())
        assert resp.status_code == 404

    def test_publish_already_active(self, client, db):
        exam = make_exam_in_db(db, status="active")
        resp = client.post(f"/api/v1/exams/{exam.id}/publish", json=self._lesson())
        assert resp.status_code == 409


# ═══════════════════════════════════════════════════════════════════════════
# 3. GET /api/v1/exams/{id}
# ═══════════════════════════════════════════════════════════════════════════

class TestGetExam:
    def test_get_existing_exam(self, client, db):
        exam = make_exam_in_db(db)
        resp = client.get(f"/api/v1/exams/{exam.id}")
        assert resp.status_code == 200
        assert resp.json()["exam_id"] == exam.id
        assert resp.json()["title"] == "Тестовый экзамен"

    def test_get_nonexistent_returns_404(self, client):
        resp = client.get("/api/v1/exams/DOES_NOT_EXIST")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 4. GET /api/v1/exams/by-code/{code}
# ═══════════════════════════════════════════════════════════════════════════

class TestGetExamByCode:
    def test_get_active_exam_by_code(self, client, db):
        exam = make_exam_in_db(db, status="active")
        # Manually set an access_code
        exam.access_code = "EXTEST1234"
        db.commit()

        resp = client.get("/api/v1/exams/by-code/EXTEST1234")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exam_id"] == exam.id
        assert data["status"] == "active"

    def test_get_draft_exam_by_code_returns_410(self, client, db):
        exam = make_exam_in_db(db, status="draft")
        exam.access_code = "EXDRAFT001"
        db.commit()

        resp = client.get("/api/v1/exams/by-code/EXDRAFT001")
        assert resp.status_code == 410

    def test_get_unknown_code_returns_404(self, client):
        resp = client.get("/api/v1/exams/by-code/XXNOTEXIST")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 5. POST /api/v1/exams/by-code/{code}/start
# ═══════════════════════════════════════════════════════════════════════════

class TestStartExam:
    def _create_active_exam(self, db, code="EXSTART001"):
        exam = make_exam_in_db(db, status="active", tasks=TASKS_POOL[:5])
        exam.access_code = code
        db.commit()
        return exam

    def test_start_creates_session(self, client, db):
        self._create_active_exam(db)
        resp = client.post("/api/v1/exams/by-code/EXSTART001/start", json={
            "student_name": "Асель Нурланова",
            "class_level": 8,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["variant_number"] == 1
        assert len(data["tasks"]) == 5
        assert data["time_minutes"] == 45

    def test_start_tasks_have_no_correct_answer(self, client, db):
        """Tasks returned to student must NOT expose correct_answer."""
        self._create_active_exam(db, code="EXSTART002")
        resp = client.post("/api/v1/exams/by-code/EXSTART002/start", json={
            "student_name": "Тест Студент",
        })
        for task in resp.json()["tasks"]:
            assert "correct_answer" not in task

    def test_start_closed_exam_returns_410(self, client, db):
        exam = make_exam_in_db(db, status="closed")
        exam.access_code = "EXCLOSED01"
        db.commit()
        resp = client.post("/api/v1/exams/by-code/EXCLOSED01/start", json={
            "student_name": "Тест",
        })
        assert resp.status_code == 410

    def test_start_requires_student_name(self, client, db):
        self._create_active_exam(db, code="EXSTART003")
        resp = client.post("/api/v1/exams/by-code/EXSTART003/start", json={
            "student_name": "",
        })
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# 6. POST /api/v1/exam-sessions/{id}/submit
# ═══════════════════════════════════════════════════════════════════════════

class TestSubmitExam:
    def test_submit_all_correct(self, client, db):
        tasks = [make_task(f"TASK_{i:03d}", answer=str(i * 3)) for i in range(1, 4)]
        exam = make_exam_in_db(db, tasks=tasks)
        session = make_session_in_db(db, exam)

        answers = {t["task_id"]: t["correct_answer"] for t in tasks}
        resp = client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={
            "answers": answers,
            "time_spent_seconds": 120,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["correct_count"] == 3
        assert data["total_tasks"] == 3
        assert data["score"] == 30   # 3 × 10
        assert data["percentage"] == 100.0

    def test_submit_all_wrong(self, client, db):
        tasks = [make_task("TASK_001", answer="42")]
        exam = make_exam_in_db(db, tasks=tasks)
        session = make_session_in_db(db, exam)

        resp = client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={
            "answers": {"TASK_001": "99"},
            "time_spent_seconds": 60,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["correct_count"] == 0
        assert data["percentage"] == 0.0

    def test_submit_partial(self, client, db):
        tasks = [
            make_task("TASK_001", answer="10"),
            make_task("TASK_002", answer="20"),
            make_task("TASK_003", answer="30"),
        ]
        exam = make_exam_in_db(db, tasks=tasks)
        session = make_session_in_db(db, exam)

        resp = client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={
            "answers": {"TASK_001": "10", "TASK_002": "99", "TASK_003": "30"},
        })
        data = resp.json()
        assert data["correct_count"] == 2
        assert data["percentage"] == pytest.approx(66.7, abs=0.1)

    def test_submit_with_comma_decimal(self, client, db):
        tasks = [make_task("TASK_001", answer="3.14", answer_type="decimal")]
        exam = make_exam_in_db(db, tasks=tasks)
        session = make_session_in_db(db, exam)

        resp = client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={
            "answers": {"TASK_001": "3,14"},
        })
        assert resp.json()["correct_count"] == 1

    def test_submit_twice_returns_the_first_result(self, client, db):
        """ROADMAP #95: a repeated submit (double click, lost response) is answered with the stored result, 200."""
        tasks = [make_task("TASK_001", answer="42"), make_task("TASK_002", answer="7")]
        exam = make_exam_in_db(db, tasks=tasks)
        session = make_session_in_db(db, exam)
        body = {"answers": {"TASK_001": "42", "TASK_002": "0"}, "time_spent_seconds": 30}

        first = client.post(f"/api/v1/exam-sessions/{session.id}/submit", json=body)
        second = client.post(f"/api/v1/exam-sessions/{session.id}/submit", json=body)
        assert first.status_code == 200 and second.status_code == 200
        assert second.json() == first.json()
        assert first.json()["correct_count"] == 1 and first.json()["answered_count"] == 2

    def test_submit_of_an_attempt_graded_elsewhere_returns_its_stored_result(self, client, db):
        exam = make_exam_in_db(db)
        session = make_session_in_db(db, exam, status="submitted")

        resp = client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={
            "answers": {},
        })
        assert resp.status_code == 200 and resp.json()["session_id"] == session.id

    def test_submit_requires_the_attempt_capability(self, client, db):
        """A session id alone must never be enough to submit for a student."""
        tasks = [make_task("TASK_001", answer="42")]
        exam = make_exam_in_db(db, tasks=tasks)
        session = make_session_in_db(db, exam)

        for token in ("", "someone-elses-token"):
            resp = client.post(
                f"/api/v1/exam-sessions/{session.id}/submit",
                json={"answers": {"TASK_001": "42"}},
                headers={"X-Exam-Session-Token": token},
            )
            assert resp.status_code == 403

    def test_submit_unknown_session_returns_404(self, client):
        resp = client.post("/api/v1/exam-sessions/UNKNOWN/submit", json={"answers": {}})
        assert resp.status_code == 404

    def test_submit_empty_answers(self, client, db):
        tasks = [make_task("TASK_001", answer="42")]
        exam = make_exam_in_db(db, tasks=tasks)
        session = make_session_in_db(db, exam)

        resp = client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={
            "answers": {},
        })
        assert resp.status_code == 200
        assert resp.json()["correct_count"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 7. GET /api/v1/exam-sessions/{id}/results
# ═══════════════════════════════════════════════════════════════════════════

class TestGetResults:
    def test_results_after_submit(self, client, db):
        tasks = [
            make_task("TASK_001", answer="10"),
            make_task("TASK_002", answer="20"),
        ]
        exam = make_exam_in_db(db, tasks=tasks)
        session = make_session_in_db(db, exam)

        # Submit first
        client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={
            "answers": {"TASK_001": "10", "TASK_002": "99"},
        })
        # The key is revealed only once the whole exam is over.
        exam.status = "closed"
        db.commit()
        # Now get results
        resp = client.get(f"/api/v1/exam-sessions/{session.id}/results")
        assert resp.status_code == 200
        data = resp.json()
        assert data["correct_count"] == 1
        assert data["total_tasks"] == 2
        assert len(data["tasks"]) == 2

        correct_tasks = [t for t in data["tasks"] if t["is_correct"]]
        wrong_tasks = [t for t in data["tasks"] if not t["is_correct"]]
        assert len(correct_tasks) == 1
        assert len(wrong_tasks) == 1

    def test_results_expose_correct_answer(self, client, db):
        """After submission, results SHOULD show correct_answer."""
        tasks = [make_task("TASK_001", answer="42")]
        exam = make_exam_in_db(db, tasks=tasks)
        session = make_session_in_db(db, exam)

        client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={
            "answers": {"TASK_001": "99"},
        })
        exam.status = "closed"
        db.commit()
        resp = client.get(f"/api/v1/exam-sessions/{session.id}/results")
        data = resp.json()
        assert data["answer_key_available"] is True
        task_result = data["tasks"][0]
        assert task_result["correct_answer"] == "42"
        assert task_result["student_answer"] == "99"
        assert task_result["is_correct"] is False

    def test_results_hide_key_while_exam_running(self, client, db):
        """A student who submitted early must not see the key while classmates write."""
        task = make_task("TASK_001", answer="42")
        task["correct_answer_latex"] = "42_latex"
        task["distractor_meta"] = [{"value": "99", "error_type": "x", "explanation": "SECRET_EXPL"}]
        exam = make_exam_in_db(db, tasks=[task])
        session = make_session_in_db(db, exam)
        client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={
            "answers": {"TASK_001": "99"},
        })
        assert exam.status == "active"
        resp = client.get(f"/api/v1/exam-sessions/{session.id}/results")
        assert resp.status_code == 200
        data = resp.json()
        assert data["answer_key_available"] is False
        assert data["message"]
        item = data["tasks"][0]
        assert item["student_answer"] == "99"
        assert item["correct_answer"] is None
        assert item["correct_answer_latex"] is None
        assert item["is_correct"] is None
        assert item["distractor_hit"] is None
        assert "SECRET_EXPL" not in resp.text and "42_latex" not in resp.text

    def test_results_key_shown_after_teacher_closes_exam(self, client, db):
        exam = make_exam_in_db(db, tasks=[make_task("TASK_001", answer="42")])
        session = make_session_in_db(db, exam)
        client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={"answers": {"TASK_001": "1"}})
        assert client.get(f"/api/v1/exam-sessions/{session.id}/results").json()["answer_key_available"] is False
        assert client.post(f"/api/v1/exams/{exam.id}/close").status_code == 200
        data = client.get(f"/api/v1/exam-sessions/{session.id}/results").json()
        assert data["answer_key_available"] is True
        assert data["tasks"][0]["correct_answer"] == "42"
        assert data["tasks"][0]["is_correct"] is False

    def test_teacher_draft_still_shows_key_while_running(self, client, db):
        exam = make_exam_in_db(db, tasks=[make_task("TASK_001", answer="42")])
        resp = client.get(f"/api/v1/exams/{exam.id}/draft")
        assert resp.status_code == 200
        assert "42" in resp.text

    def test_results_before_submit_returns_400(self, client, db):
        exam = make_exam_in_db(db)
        session = make_session_in_db(db, exam, status="in_progress")
        resp = client.get(f"/api/v1/exam-sessions/{session.id}/results")
        assert resp.status_code == 400

    def test_results_unknown_session_returns_404(self, client):
        resp = client.get("/api/v1/exam-sessions/UNKNOWN/results")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 8. POST /api/v1/exams/{id}/close
# ═══════════════════════════════════════════════════════════════════════════

class TestCloseExam:
    def test_close_active_exam(self, client, db):
        exam = make_exam_in_db(db, status="active")
        resp = client.post(f"/api/v1/exams/{exam.id}/close")
        assert resp.status_code == 200
        assert resp.json()["status"] == "closed"

    def test_close_nonexistent_returns_404(self, client):
        resp = client.post("/api/v1/exams/NOEXIST/close")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 9. GET /api/v1/exams/{id}/sessions
# ═══════════════════════════════════════════════════════════════════════════

class TestListSessions:
    def test_list_sessions_empty(self, client, db):
        exam = make_exam_in_db(db, status="active")
        resp = client.get(f"/api/v1/exams/{exam.id}/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_sessions_one(self, client, db):
        exam = make_exam_in_db(db, status="active")
        make_session_in_db(db, exam)
        resp = client.get(f"/api/v1/exams/{exam.id}/sessions")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_list_sessions_nonexistent_exam(self, client):
        resp = client.get("/api/v1/exams/NOEXIST/sessions")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 10. Full student journey (E2E)
# ═══════════════════════════════════════════════════════════════════════════

class TestFullStudentJourney:
    def test_generate_publish_start_submit_results(self, client, db):
        # 1. Teacher generates exam
        with patch("src.api.router.TaskRepository", return_value=mock_task_repo(TASKS_POOL[:3])):
            gen = client.post("/api/v1/exams/generate", json={
                "title": "E2E тест",
                "skill_ids": ["SK_001"],
                "tasks_count": 3,
                "time_minutes": 20,
            })
        assert gen.status_code == 200
        exam_id = gen.json()["exam_id"]

        # 2. Teacher publishes it onto a concrete lesson — this opens the lobby
        pub = client.post(f"/api/v1/exams/{exam_id}/publish", json={
            "teacher_name": "Мария Петровна",
            "group_id": TEST_GROUP_ID,
            "lesson_id": TEST_LESSON_ID,
            "lesson_date": _date.today().isoformat(),
        })
        assert pub.status_code == 200
        assert pub.json()["status"] == "lobby"
        code = pub.json()["access_code"]
        assert code.startswith("EX")

        # 3. Teacher starts the lesson run — only now is the exam active
        live = client.post(f"/api/v1/exams/{exam_id}/start-live")
        assert live.status_code == 200

        # 4. Student sees exam info
        info = client.get(f"/api/v1/exams/by-code/{code}")
        assert info.status_code == 200
        assert info.json()["status"] == "active"

        # 5. Student starts
        start = client.post(f"/api/v1/exams/by-code/{code}/start", json={
            "student_name": "Болат Ахметов",
            "class_level": 9,
        })
        assert start.status_code == 200
        session_id = start.json()["session_id"]
        # Every later request for this attempt is authorised by its capability.
        session_token = start.json()["session_token"]
        assert session_token
        headers = {"X-Exam-Session-Token": session_token}
        tasks = start.json()["tasks"]
        assert len(tasks) == 3
        # No correct_answer exposed
        for t in tasks:
            assert "correct_answer" not in t

        # 6. Student submits — answer all correctly from TASKS_POOL
        # We know the correct answers from our fixture
        answers = {}
        for t in TASKS_POOL[:3]:
            answers[t["task_id"]] = t["correct_answer"]

        sub = client.post(
            f"/api/v1/exam-sessions/{session_id}/submit",
            json={"answers": answers, "time_spent_seconds": 300},
            headers=headers,
        )
        assert sub.status_code == 200
        sub_data = sub.json()
        assert sub_data["correct_count"] == 3
        assert sub_data["percentage"] == 100.0

        # 7. Student views results: key is hidden while the exam is running
        res = client.get(f"/api/v1/exam-sessions/{session_id}/results", headers=headers)
        assert res.status_code == 200
        res_data = res.json()
        assert res_data["student_name"] == "Болат Ахметов"
        assert res_data["correct_count"] == 3
        assert res_data["answer_key_available"] is False
        assert all(t["correct_answer"] is None for t in res_data["tasks"])

        # 7. Teacher closes
        close = client.post(f"/api/v1/exams/{exam_id}/close")
        assert close.status_code == 200
        assert close.json()["status"] == "closed"

        # 7b. After the exam ends the full review is available
        res = client.get(f"/api/v1/exam-sessions/{session_id}/results", headers=headers)
        res_data = res.json()
        assert res_data["answer_key_available"] is True
        assert all(t["is_correct"] for t in res_data["tasks"])

        # 8. Student can no longer start after close
        start_after_close = client.post(f"/api/v1/exams/by-code/{code}/start", json={
            "student_name": "Другой студент",
        })
        assert start_after_close.status_code == 410
