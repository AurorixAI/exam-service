"""Wave B / P1 - exams: the pass threshold belongs to the exam, one aggregation for «Аналитика» and «Отчёт», a library without a
ceiling of 100 (EX-01, EX-04, EX-11, EX-22, EX-28, EX-44, EX-45).  Behavioural: every test names the mutation it kills.

No real clock (LESSONS 76): exam dates are fixed constants.  No paid model: ``call_gemini`` is replaced by a recorder.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.core import auth as core_auth
from src.core.auth import ExamActor, require_exam_staff, require_heavy_staff
from src.core.database import get_db
from src.domain import report_generator
from src.domain.exam_aggregate import aggregate_exam, exam_pass_threshold_pct, has_passed
from src.models.exam_models import ExamReport, ExamSession, GeneratedExam
from tests.conftest import (
    TEST_GROUP_ID,
    TEST_ORG_ID,
    TEST_USER_ID,
    make_actor,
    make_exam_in_db,
    make_task,
    session_headers,
    session_token_for,
)

API = "/api/v1"
MESSAGE_STARTED = "Порог нельзя изменить: экзамен уже начался"
BASE = datetime(2026, 9, 1, 8, 0, 0)  # fixed: the order of the library never depends on today's date


# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# helpers
# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _tasks(count: int) -> list[dict]:
    return [make_task(f"T{i:02d}", answer=str(i)) for i in range(1, count + 1)]


def _exam(db, tasks=None, *, threshold=None, exam_id="EXAM_WB01", status="active", variants=None) -> GeneratedExam:
    exam = make_exam_in_db(db, tasks=tasks or _tasks(10), status=status)
    if exam_id != exam.id:
        exam.id = exam_id
    if threshold is not None:
        exam.pass_threshold_pct = threshold
    if variants is not None:
        exam.variants = variants
    db.commit()
    return exam


def _submit(client, db, exam, name: str, correct: int, *, variant=1, total=None) -> ExamSession:
    """A real student: an attempt in progress, then ``/submit`` with the first ``correct`` answers right and nothing else touched."""
    tasks = next(v for v in exam.variants if v["variant_number"] == variant)["tasks"]
    sid = f"SES_{name}"
    db.add(ExamSession(
        id=sid, exam_id=exam.id, student_name=name, variant_number=variant, answers={},
        max_score=len(tasks) * 10, status="in_progress", session_token=session_token_for(sid),
    ))
    db.commit()
    answers = {t["task_id"]: t["correct_answer"] for t in tasks[:correct]}
    response = client.post(f"{API}/exam-sessions/{sid}/submit", json={"answers": answers, "time_spent_seconds": 60}, headers=session_headers(sid))
    assert response.status_code == 200, response.text
    return db.get(ExamSession, sid)


@pytest.fixture
def recorder(monkeypatch):
    """The model of the report: nothing is paid, the prompt is kept for the assertions."""
    prompts: list[str] = []

    def fake(prompt, **kwargs):
        prompts.append(prompt)
        return json.dumps({"summary": "итог", "strengths": [], "critical_gaps": [], "problem_tasks_analysis": []})

    monkeypatch.setattr(report_generator, "call_gemini", fake)
    monkeypatch.setattr(report_generator, "narrative_enabled", lambda: True)
    report_generator._in_flight.clear()
    return prompts


# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# 1. the pass threshold belongs to the exam
# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_has_passed_is_inclusive_and_exact():
    """Kills `>` instead of `>=` and float drift: 70 % of 10 points is exactly 7 points."""
    assert has_passed(7, 10, 70) is True
    assert has_passed(6, 10, 70) is False
    assert has_passed(70, 100, 70) is True
    assert has_passed(69, 100, 70) is False
    assert has_passed(10, 10, 100) is True
    assert has_passed(0, 0, 60) is False          # no maximum - nobody passed
    assert has_passed(None, 10, 1) is False


def test_an_exam_without_the_column_value_counts_60():
    assert exam_pass_threshold_pct(GeneratedExam()) == 60
    assert exam_pass_threshold_pct(GeneratedExam(pass_threshold_pct=85)) == 85


def _generate(client, **extra):
    pool = [make_task(f"G{i:02d}", answer=str(i)) for i in range(1, 11)]
    repo = MagicMock()
    repo.get_tasks_by_skills.return_value = pool
    with patch("src.api.router.TaskRepository", return_value=repo):
        return client.post(f"{API}/exams/generate", json={"title": "Контрольная", "skill_ids": ["SK_001"], "tasks_count": 5, **extra})


def test_a_new_exam_is_60_by_default_and_keeps_its_own_threshold(client, db):
    default = _generate(client)
    assert default.status_code == 200
    own = _generate(client, pass_threshold_pct=70)
    assert own.status_code == 200
    assert db.get(GeneratedExam, default.json()["exam_id"]).pass_threshold_pct == 60
    own_id = own.json()["exam_id"]
    assert db.get(GeneratedExam, own_id).pass_threshold_pct == 70
    assert client.get(f"{API}/exams/{own_id}").json()["pass_threshold_pct"] == 70
    draft = client.get(f"{API}/exams/{own_id}/draft").json()
    assert draft["pass_threshold_pct"] == 70 and draft["pass_threshold_locked"] is False


@pytest.mark.parametrize("bad", [0, -1, 101, 1000, "много"])
def test_creation_refuses_a_threshold_outside_1_100(client, bad):
    assert _generate(client, pass_threshold_pct=bad).status_code == 422


@pytest.mark.parametrize("edge", [1, 100])
def test_creation_accepts_the_edges(client, edge):
    assert _generate(client, pass_threshold_pct=edge).status_code == 200


def test_the_database_itself_refuses_a_threshold_outside_1_100(db):
    """The CHECK is the last guard (a script, a later endpoint): kills a model without the constraint."""
    for bad in (0, 101):
        row = GeneratedExam(
            id=f"EXAM_BAD{bad}", title="x", variants=[], settings={}, org_id=TEST_ORG_ID, created_by=TEST_USER_ID,
            status="draft", pass_threshold_pct=bad,
        )
        db.add(row)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


def test_a_clone_inherits_the_threshold(client, db, actor):
    exam = _exam(db, threshold=75, status="closed")
    response = client.post(f"{API}/exams/{exam.id}/clone", json={})
    assert response.status_code == 200, response.text
    assert db.get(GeneratedExam, response.json()["exam_id"]).pass_threshold_pct == 75


@pytest.mark.parametrize("status", ["draft", "variants_ready", "lobby"])
def test_threshold_can_change_before_the_exam_begins(client, db, status):
    """Owner's rule: a draft and a lobby may still change the threshold; students waiting in the lobby do not lock it."""
    exam = _exam(db, threshold=60, status=status)
    assert client.patch(f"{API}/exams/{exam.id}/pass-threshold", json={"pass_threshold_pct": 70}).json() == {
        "exam_id": exam.id, "pass_threshold_pct": 70,
    }
    db.expire_all()
    assert db.get(GeneratedExam, exam.id).pass_threshold_pct == 70
    assert client.get(f"{API}/exams/{exam.id}/draft").json()["pass_threshold_locked"] is False
    db.add(ExamSession(id="SES_waiting", exam_id=exam.id, student_name="w", variant_number=1, answers={}, max_score=100, status="waiting", session_token="tok-w"))
    db.commit()
    again = client.patch(f"{API}/exams/{exam.id}/pass-threshold", json={"pass_threshold_pct": 80})
    assert again.status_code == 200 and again.json()["pass_threshold_pct"] == 80


@pytest.mark.parametrize("status", ["active", "closed", "archived", "print_ready"])
def test_threshold_is_shut_once_the_exam_is_not_a_draft_or_a_lobby(client, db, status):
    """ONE rule: every status but draft / variants_ready / lobby answers 409 with ONE text; the flag of the draft answer agrees.  Kills the status rule being removed or narrowed."""
    exam = _exam(db, threshold=60, status=status)
    response = client.patch(f"{API}/exams/{exam.id}/pass-threshold", json={"pass_threshold_pct": 90})
    assert response.status_code == 409
    assert response.json()["detail"] == MESSAGE_STARTED
    assert response.json()["code"] == "PASS_THRESHOLD_LOCKED"
    db.expire_all()
    assert db.get(GeneratedExam, exam.id).pass_threshold_pct == 60
    assert client.get(f"{API}/exams/{exam.id}/draft").json()["pass_threshold_locked"] is True
    assert client.patch(f"{API}/exams/{exam.id}/pass-threshold", json={"pass_threshold_pct": 60}).status_code == 200   # the same number is not a change


def test_a_submitted_work_does_not_add_a_second_rule_or_a_second_text(client, db):
    """The older «есть сданная работа» rule is gone: the status decides, and the text is the same one."""
    exam = _exam(db, threshold=70)                                            # active
    _submit(client, db, exam, "ANVAR", 5)
    response = client.patch(f"{API}/exams/{exam.id}/pass-threshold", json={"pass_threshold_pct": 50})
    assert response.status_code == 409 and response.json()["detail"] == MESSAGE_STARTED
    # and a lobby that (by a defect elsewhere) held a submitted row would still be open: the rule looks at the status only
    exam.status = "lobby"
    db.commit()
    assert client.patch(f"{API}/exams/{exam.id}/pass-threshold", json={"pass_threshold_pct": 50}).status_code == 200


def test_a_running_exam_whose_time_is_up_is_shut_like_any_running_exam(client, db):
    exam = _exam(db, threshold=60)
    exam.settings = {**exam.settings, "started_at": (datetime.utcnow() - timedelta(minutes=exam.total_time_minutes + 5)).isoformat()}
    db.add(ExamSession(id="SES_LATE", exam_id=exam.id, student_name="Late", variant_number=1, answers={}, max_score=10, status="in_progress", session_token="tok-late"))
    db.commit()
    response = client.patch(f"{API}/exams/{exam.id}/pass-threshold", json={"pass_threshold_pct": 90})
    assert response.status_code == 409 and response.json()["detail"] == MESSAGE_STARTED
    assert db.get(ExamSession, "SES_LATE").status == "in_progress"           # the request closes nothing: it only refuses


# ── the invariant behind the one rule: «a submitted work exists ⇒ the status is not draft / variants_ready / lobby» ─────────────────────────────

def _assert_no_started_exam_looks_unstarted(db):
    """The whole database: no draft / variants_ready / lobby exam holds a submitted (or in-progress) work."""
    db.expire_all()
    bad = (
        db.query(GeneratedExam.id, GeneratedExam.status)
        .join(ExamSession, ExamSession.exam_id == GeneratedExam.id)
        .filter(GeneratedExam.status.in_(["draft", "variants_ready", "lobby"]), ExamSession.status.in_(["submitted", "in_progress"]))
        .all()
    )
    assert bad == []


def test_invariant_a_work_is_submitted_only_in_a_running_exam(client, db):
    """submit refuses in a lobby (410): the only way to a submitted work is an active exam."""
    exam = _exam(db, status="lobby")
    db.add(ExamSession(id="SES_LOBBY", exam_id=exam.id, student_name="w", variant_number=1, answers={}, max_score=10, status="waiting", session_token=session_token_for("SES_LOBBY")))
    db.commit()
    response = client.post(f"{API}/exam-sessions/SES_LOBBY/submit", json={"answers": {}, "time_spent_seconds": 1}, headers=session_headers("SES_LOBBY"))
    assert response.status_code in (409, 410)
    _assert_no_started_exam_looks_unstarted(db)


def test_invariant_no_path_moves_an_exam_with_works_back_to_a_draft_or_a_lobby(client, db, monkeypatch):
    """Every road back (cancel delivery, the lesson cancelled, unbind by «assign», restore from the archive, clone) either refuses or leaves a held exam closed."""
    from src.api import router

    held = _exam(db, _tasks(5), threshold=70)                                 # active, with a submitted work
    _submit(client, db, held, "ANVAR", 3)
    # 1. the teacher cancels the delivery
    assert client.post(f"{API}/exams/{held.id}/delivery/cancel", json={"reason": "Урок отменён"}).status_code in (409, 422)
    # 2. the lesson is cancelled / the recurrence ends (internal calls of auth-service): a started exam blocks them
    monkeypatch.setattr(router, "_require_internal_service_token", lambda token: None)
    body = {"org_id": TEST_ORG_ID, "group_id": held.group_id, "lesson_id": held.scheduled_lesson_id, "lesson_date": held.scheduled_lesson_date, "reason": "Болезнь"}
    assert client.post(f"{API}/internal/lesson-bindings/release", json=body).status_code == 409
    # 3. «assign» without a lesson (unbind) or to another group
    assert client.post(f"{API}/exams/{held.id}/assign", json={"group_id": TEST_GROUP_ID}).status_code == 409
    _assert_no_started_exam_looks_unstarted(db)
    # 4. closed -> archived -> restored: it comes back CLOSED (held), never as a draft
    assert client.post(f"{API}/exams/{held.id}/close").status_code == 200
    assert client.delete(f"{API}/exams/{held.id}").status_code == 200
    restored = client.post(f"{API}/exams/{held.id}/restore")
    assert restored.status_code == 200 and restored.json()["status"] == "closed"
    # 5. an exam that was archived WITHOUT any attempt comes back as a draft - and has no work to lose
    fresh = _exam(db, exam_id="EXAM_NOWORK", status="archived")
    assert client.post(f"{API}/exams/{fresh.id}/restore").json()["status"] in ("draft", "variants_ready")
    # 6. a clone is a new exam without sessions
    clone_id = client.post(f"{API}/exams/{held.id}/clone", json={}).json()["exam_id"]
    assert db.query(ExamSession).filter(ExamSession.exam_id == clone_id).count() == 0
    _assert_no_started_exam_looks_unstarted(db)


def test_invariant_print_exams_have_no_attempt_rows(db):
    """Results of a printed exam are teacher-checked on paper: the only code that creates an ExamSession is the public online start, which refuses a print exam (410)."""
    import inspect
    from src.api import router

    creators = [name for name, fn in inspect.getmembers(router, inspect.isfunction) if "ExamSession(" in inspect.getsource(fn)]
    assert creators == ["_admit_student_to_exam"]


@pytest.mark.parametrize("bad", [0, 101, "x"])
def test_patch_refuses_a_threshold_outside_1_100(client, db, bad):
    exam = _exam(db)
    assert client.patch(f"{API}/exams/{exam.id}/pass-threshold", json={"pass_threshold_pct": bad}).status_code == 422


def test_only_the_groups_teacher_changes_the_threshold(client, db):
    from src.main import app

    exam = _exam(db)
    app.dependency_overrides[require_exam_staff] = lambda: make_actor("admin")
    assert client.patch(f"{API}/exams/{exam.id}/pass-threshold", json={"pass_threshold_pct": 70}).status_code == 403
    foreign = ExamActor(user_id="u-x", org_id="ORG_OTHER", role="teacher", auth_headers={})
    app.dependency_overrides[require_exam_staff] = lambda: foreign
    assert client.patch(f"{API}/exams/{exam.id}/pass-threshold", json={"pass_threshold_pct": 70}).status_code == 404
    db.expire_all()
    assert db.get(GeneratedExam, exam.id).pass_threshold_pct == 60


# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# 2. one number in «Аналитика», «Отчёт» and the AI text (EX-11, EX-22)
# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_threshold_70_two_students_55_and_75_give_50_percent_everywhere(client, db, recorder):
    """The acceptance scenario of the owner: 55 % and 75 % at threshold 70 -> one of two passed, in the tab, in the report, in the AI prompt."""
    exam = _exam(db, _tasks(20), threshold=70)
    _submit(client, db, exam, "LOW", 11)     # 55 %
    _submit(client, db, exam, "HIGH", 15)    # 75 %

    analytics = client.get(f"{API}/exams/{exam.id}/analytics").json()
    assert analytics["pass_rate_pct"] == 50.0
    assert analytics["pass_threshold_pct"] == 70
    assert analytics["avg_score_pct"] == 65.0

    report = client.post(f"{API}/exams/{exam.id}/report").json()
    assert report["pass_rate_pct"] == 50.0
    assert report["pass_threshold_pct"] == 70
    assert report["avg_score_pct"] == analytics["avg_score_pct"]
    assert len(recorder) == 1
    assert "Процент преодолевших порог (≥70%): 50.0%" in recorder[0]
    assert "≥60%" not in recorder[0]
    assert client.get(f"{API}/exams/{exam.id}/report").json()["pass_threshold_pct"] == 70


def test_the_same_results_pass_or_fail_by_the_exams_own_threshold(client, db):
    """Kills the constants 0.5 / 0.6: 55 % passes at 50, fails at 60 and at the default."""
    exam = _exam(db, _tasks(20), threshold=50)
    _submit(client, db, exam, "MID", 11)
    assert client.get(f"{API}/exams/{exam.id}/analytics").json()["pass_rate_pct"] == 100.0
    exam.pass_threshold_pct = 56
    db.commit()
    assert client.get(f"{API}/exams/{exam.id}/analytics").json()["pass_rate_pct"] == 0.0
    exam.pass_threshold_pct = 55                       # exactly at the threshold - passed
    db.commit()
    assert client.get(f"{API}/exams/{exam.id}/analytics").json()["pass_rate_pct"] == 100.0


def test_the_fallback_text_names_the_exams_threshold(db):
    exam = _exam(db, _tasks(10), threshold=85)
    agg = aggregate_exam(exam, [])
    agg.update(submitted_count=3, pass_rate_pct=33.3, avg_score_pct=70.0)
    text = report_generator._build_deterministic_fallback("Работа", 8, agg)["summary"]
    assert "≥85%" in text and "≥60%" not in text


# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# 3. one aggregation: the denominator is every task of the student's variant (EX-04, EX-11)
# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_analytics_and_report_show_the_same_numbers_when_tasks_were_skipped(client, db, recorder):
    """20 students, task T02 solved by 2, skipped by 18: the old tab said 2 of 2 = 100 %, the report 2 of 20 = 10 %."""
    exam = _exam(db, _tasks(3))
    for i in range(20):
        # the first 2 students solve T01..T02 (T02 right), the other 18 touch nothing
        _submit(client, db, exam, f"S{i:02d}", 2 if i < 2 else 0)

    analytics = client.get(f"{API}/exams/{exam.id}/analytics").json()
    by_task = {t["task_id"]: t for t in analytics["task_analytics"]}
    assert set(by_task) == {"T01", "T02", "T03"}        # T03 nobody solved or touched - it must still be listed
    assert by_task["T02"]["attempts"] == 20 and by_task["T02"]["correct_count"] == 2
    assert by_task["T02"]["correct_rate"] == 0.1
    assert by_task["T03"]["attempts"] == 20 and by_task["T03"]["correct_rate"] == 0.0
    assert analytics["task_analytics"][0]["correct_rate"] == 0.0     # «самые провальные» come first

    report = client.post(f"{API}/exams/{exam.id}/report").json()
    mine = {t["task_id"]: (t["attempts"], t["correct_count"], t["correct_rate"], t["skill_name"]) for t in analytics["task_analytics"]}
    theirs = {t["task_id"]: (t["attempts"], t["correct_count"], t["correct_rate"], t["skill_name"]) for t in report["task_analytics"]}
    assert mine == theirs
    assert analytics["pass_rate_pct"] == report["pass_rate_pct"]
    assert analytics["avg_score_pct"] == report["avg_score_pct"]
    assert analytics["top_errors"] == report["top_errors"]


def test_the_denominator_follows_each_students_own_variant(client, db):
    variants = [
        {"variant_number": 1, "tasks": [make_task("A1", answer="1"), make_task("A2", answer="2")]},
        {"variant_number": 2, "tasks": [make_task("B1", answer="1")]},
    ]
    exam = _exam(db, variants=variants)
    _submit(client, db, exam, "V1", 2, variant=1)
    _submit(client, db, exam, "V2", 0, variant=2)
    by_task = {t["task_id"]: t for t in client.get(f"{API}/exams/{exam.id}/analytics").json()["task_analytics"]}
    assert by_task["A1"]["attempts"] == 1 and by_task["A2"]["attempts"] == 1       # not 2: the second student never had A1/A2
    assert by_task["B1"]["attempts"] == 1 and by_task["B1"]["correct_count"] == 0


def test_a_missing_skill_name_reads_the_same_in_both_places(client, db):
    task = make_task("T01", answer="1")
    task["skill_name"] = None
    exam = _exam(db, [task])
    _submit(client, db, exam, "ONE", 0)
    assert client.get(f"{API}/exams/{exam.id}/analytics").json()["task_analytics"][0]["skill_name"] == "Задача"


def test_analytics_of_an_exam_without_submitted_works_is_empty(client, db):
    exam = _exam(db)
    body = client.get(f"{API}/exams/{exam.id}/analytics").json()
    assert body["submitted_sessions"] == 0 and body["pass_rate_pct"] == 0.0 and body["task_analytics"] == []
    assert body["pass_threshold_pct"] == 60


def test_a_saved_report_is_not_recomputed_by_reading_it(client, db, recorder):
    exam = _exam(db, _tasks(10))
    _submit(client, db, exam, "ONE", 10)
    first = client.post(f"{API}/exams/{exam.id}/report").json()
    saved = db.query(ExamReport).filter_by(exam_id=exam.id).one()
    saved.pass_rate_pct = 12.3
    db.commit()
    assert client.get(f"{API}/exams/{exam.id}/report").json()["pass_rate_pct"] == 12.3
    assert first["pass_rate_pct"] == 100.0


# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# 4. the library: access is decided BEFORE the page limit (EX-01)
# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

TEACHER_A, TEACHER_B = "USR_TEACHER_A", "USR_TEACHER_B"
GROUP_A = "aaaaaaaa-0000-0000-0000-00000000000a"
GROUP_B = "bbbbbbbb-0000-0000-0000-00000000000b"
GROUP_OWNER = {GROUP_A: TEACHER_A, GROUP_B: TEACHER_B}


def _row(exam_id, *, created_by, group_id=None, n=0, lesson=None, status="draft") -> GeneratedExam:
    return GeneratedExam(
        id=exam_id, title=f"Экзамен {exam_id}", variants=[], settings={}, org_id=TEST_ORG_ID, created_by=created_by,
        group_id=group_id, group_name=("Группа" if group_id else None), status=status, variants_count=1, tasks_per_variant=1,
        total_time_minutes=45, created_at=BASE + timedelta(minutes=n),
        scheduled_lesson_id=lesson[0] if lesson else None, scheduled_lesson_date=lesson[1] if lesson else None,
    )


def _as(user_id: str, role: str = "teacher"):
    from src.main import app

    actor = ExamActor(user_id=user_id, org_id=TEST_ORG_ID, role=role, auth_headers={})
    app.dependency_overrides[require_exam_staff] = lambda: actor
    app.dependency_overrides[require_heavy_staff] = lambda: actor
    return actor


@pytest.fixture
def two_teachers(db, monkeypatch):
    """150 exams of two teachers: each owns 15 library templates and 60 exams of his group.  A's exams are the OLDEST, so a
    «newest 100 of the whole centre, then filter» library (the old code) shows A nothing at all."""
    calls: list[str] = []

    async def teacher_groups(actor):
        calls.append(actor.user_id)
        return {group for group, owner in GROUP_OWNER.items() if owner == actor.user_id}

    async def no_substitution(actor):
        return {}

    monkeypatch.setattr(core_auth, "teacher_group_ids", teacher_groups)
    monkeypatch.setattr(core_auth, "assigned_substitution_lessons", no_substitution)
    rows = []
    for index in range(75):
        rows.append(_row(f"EXA_{index:03d}", created_by=TEACHER_A, group_id=GROUP_A if index < 60 else None, n=index))
    for index in range(75):
        rows.append(_row(f"EXB_{index:03d}", created_by=TEACHER_B, group_id=GROUP_B if index < 60 else None, n=1000 + index))
    db.add_all(rows)
    db.commit()
    return calls


def _all_pages(client, size=50, **params):
    ids, page = [], 1
    while True:
        body = client.get(f"{API}/exams", params={"page": page, "size": size, **params}).json()
        ids.extend(item["exam_id"] for item in body["items"])
        assert body["total"] == body["total"] and body["page"] == page and body["size"] == size
        if not body["has_more"]:
            return ids, body["total"]
        page += 1


def test_each_teacher_sees_all_75_of_his_exams_through_show_more(client, two_teachers):
    for user, prefix in ((TEACHER_A, "EXA_"), (TEACHER_B, "EXB_")):
        _as(user)
        ids, total = _all_pages(client)
        assert total == 75
        assert len(ids) == 75 and len(set(ids)) == 75           # nothing lost, nothing twice across pages
        assert all(i.startswith(prefix) for i in ids)           # and nothing of the other teacher's


def test_the_first_page_is_full_and_has_more(client, two_teachers):
    _as(TEACHER_A)
    first = client.get(f"{API}/exams", params={"page": 1, "size": 50}).json()
    second = client.get(f"{API}/exams", params={"page": 2, "size": 50}).json()
    assert len(first["items"]) == 50 and first["has_more"] is True and first["total"] == 75
    assert len(second["items"]) == 25 and second["has_more"] is False
    assert not {i["exam_id"] for i in first["items"]} & {i["exam_id"] for i in second["items"]}


def test_a_teacher_gets_every_own_exam_even_when_they_are_older_than_the_newest_100_of_the_centre(client, two_teachers):
    """Kills «limit first, filter after»: the legacy plain array (no page) used to hold only the centre's newest 100 -> none of A's."""
    _as(TEACHER_A)
    legacy = client.get(f"{API}/exams").json()
    assert isinstance(legacy, list) and len(legacy) == 75 and all(e["exam_id"].startswith("EXA_") for e in legacy)
    _as(TEACHER_B)
    assert len(client.get(f"{API}/exams").json()) == 75


def test_include_meta_is_open_to_a_teacher_and_filtered_too(client, two_teachers):
    _as(TEACHER_A)
    body = client.get(f"{API}/exams", params={"include_meta": "true", "limit": 30, "offset": 60}).json()
    assert body["total"] == 75 and len(body["items"]) == 15


def test_the_administrator_sees_the_whole_centre_in_pages(client, two_teachers):
    _as("USR_ADMIN", "admin")
    ids, total = _all_pages(client, size=100)
    assert total == 150 and len(set(ids)) == 150
    assert not two_teachers                                        # no group lookups for an administrator


def test_filters_apply_with_the_access_rule(client, two_teachers):
    _as(TEACHER_A)
    body = client.get(f"{API}/exams", params={"page": 1, "size": 100, "unassigned": "true"}).json()
    assert body["total"] == 15 and all(i["group_id"] is None for i in body["items"])
    body = client.get(f"{API}/exams", params={"page": 1, "size": 100, "group_id": GROUP_B}).json()
    assert body["total"] == 0                                      # B's group is not his, whatever he asks for


def _auth_transport(groups: list[str], assigned: list[dict], seen: list[str], *, groups_status=200, mine_status=200):
    """auth-service as the library sees it: only the two list routes exist - any other path (a lookup per group) answers 500."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path + (f"?{request.url.query.decode()}" if request.url.query else ""))
        if request.url.path.endswith("/auth/org/groups"):
            return httpx.Response(groups_status, json=[{"id": g} for g in groups])
        if request.url.path.endswith("/auth/org/substitutions/mine"):
            # the NEW auth-service: ``past_days`` widens the window back; the older one ignores the parameter and answers from today
            wanted = int(request.url.params.get("past_days", 0))
            return httpx.Response(mine_status, json={"assignedToMe": [a for a in assigned if a.get("daysAgo", 0) <= wanted]})
        return httpx.Response(500)

    return httpx.MockTransport(handler)


def _centre_of_50_groups(db):
    """50 groups, each with 3 exams bound to a lesson; teacher A teaches the first 3."""
    ids = [f"cccccccc-0000-0000-0000-{i:012d}" for i in range(50)]
    rows = [
        _row(f"EXC_{g:02d}_{k}", created_by="USR_BOSS", group_id=group, n=g * 10 + k, lesson=(f"lesson-{k}", "2026-11-03"), status="lobby")
        for g, group in enumerate(ids) for k in range(3)
    ]
    db.add_all(rows)
    db.commit()
    return ids


def test_a_centre_of_50_groups_costs_the_teacher_two_calls_to_auth_per_page(client, db, monkeypatch):
    """R3 #1: the library used to ask auth for every group of the centre (and the lesson of each foreign one): 97 calls per page at 50 groups.  Kills a per-group lookup."""
    ids = _centre_of_50_groups(db)
    seen: list[str] = []
    real = httpx.AsyncClient
    monkeypatch.setattr(core_auth.httpx, "AsyncClient", lambda *a, **k: real(*a, transport=_auth_transport(ids[:3], [], seen), **k))
    _as(TEACHER_A)
    first = client.get(f"{API}/exams", params={"page": 1, "size": 5}).json()
    assert first["total"] == 9 and len(first["items"]) == 5 and {i["group_id"] for i in first["items"]} <= set(ids[:3])
    assert len(seen) == 2                              # the groups he teaches + his substitute lessons: at most 3, nothing per group
    assert any(path.endswith("/auth/org/substitutions/mine?past_days=7") for path in seen)
    seen.clear()
    second = client.get(f"{API}/exams", params={"page": 2, "size": 5}).json()
    assert len(second["items"]) == 4 and len(seen) == 2    # «Показать ещё» asks the same two again, not more


def test_a_failing_substitute_list_leaves_those_lessons_out_but_never_refuses_the_library(client, db, monkeypatch, caplog):
    ids = _centre_of_50_groups(db)
    real = httpx.AsyncClient
    assigned = [{"groupId": ids[10], "lessonId": "lesson-1", "lessonDate": "2026-11-03"}]
    totals = {}
    for mine_status in (200, 500):
        monkeypatch.setattr(
            core_auth.httpx, "AsyncClient",
            lambda *a, _s=mine_status, **k: real(*a, transport=_auth_transport(ids[:3], assigned, [], mine_status=_s), **k),
        )
        _as(TEACHER_A)
        with caplog.at_level("WARNING", logger="exam_service.auth"):
            totals[mine_status] = client.get(f"{API}/exams", params={"page": 1, "size": 50}).json()["total"]
    assert totals == {200: 9 + 1, 500: 9}                      # the one lesson he substitutes in is listed, and only when auth answers
    assert "substitute lessons of user" in caplog.text           # the failure is logged, not silent


def test_a_substitute_sees_the_exam_of_yesterdays_lesson_in_the_library(client, db, monkeypatch):
    """The owner's decision: a substitute keeps seeing, for 7 days, the exam of a lesson he led (what ``lesson-access`` always gave him)."""
    ids = _centre_of_50_groups(db)
    real = httpx.AsyncClient
    yesterday = {"groupId": ids[20], "lessonId": "lesson-0", "lessonDate": "2026-11-03", "daysAgo": 1}
    today = {"groupId": ids[21], "lessonId": "lesson-1", "lessonDate": "2026-11-04", "daysAgo": 0}
    monkeypatch.setattr(core_auth.httpx, "AsyncClient", lambda *a, **k: real(*a, transport=_auth_transport(ids[:3], [yesterday, today], [], ), **k))
    db.get(GeneratedExam, "EXC_21_1").scheduled_lesson_date = "2026-11-04"      # (EXC_20_0 is bound to lesson-0 of 2026-11-03 already)
    db.commit()
    _as(TEACHER_A)
    body = client.get(f"{API}/exams", params={"page": 1, "size": 50}).json()
    ours = {i["exam_id"] for i in body["items"]}
    assert {"EXC_20_0", "EXC_21_1"} <= ours                                  # yesterday's lesson AND today's
    assert "EXC_20_1" not in ours and "EXC_22_0" not in ours                  # another lesson of that group, another group
    assert body["total"] == 9 + 2


def test_an_older_auth_service_that_ignores_past_days_still_answers_from_today(client, db, monkeypatch):
    """The window of a rolling release: exam new, auth old.  FastAPI skips an unknown query parameter, so the call works; only yesterday's lessons are missing."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    old_auth = FastAPI()

    @old_auth.get("/api/v1/auth/org/substitutions/mine")          # the route of the older auth: no ``past_days`` parameter
    def mine(status: str = "all"):
        return {"assignedToMe": [{"groupId": "g", "lessonId": "l", "lessonDate": "2026-11-04"}]}

    assert TestClient(old_auth).get("/api/v1/auth/org/substitutions/mine", params={"past_days": 7}).json() == {
        "assignedToMe": [{"groupId": "g", "lessonId": "l", "lessonDate": "2026-11-04"}]
    }
    ids = _centre_of_50_groups(db)
    real = httpx.AsyncClient
    yesterday = {"groupId": ids[20], "lessonId": "lesson-0", "lessonDate": "2026-11-03", "daysAgo": 1}
    monkeypatch.setattr(
        core_auth.httpx, "AsyncClient",
        lambda *a, **k: real(*a, transport=httpx.MockTransport(lambda r: httpx.Response(
            200, json=[{"id": g} for g in ids[:3]] if r.url.path.endswith("/groups") else {"assignedToMe": []})), **k),
    )
    _as(TEACHER_A)
    assert client.get(f"{API}/exams", params={"page": 1, "size": 50}).json()["total"] == 9


def test_without_the_list_of_his_groups_the_library_is_a_503(client, db, monkeypatch):
    ids = _centre_of_50_groups(db)
    real = httpx.AsyncClient
    monkeypatch.setattr(core_auth.httpx, "AsyncClient", lambda *a, **k: real(*a, transport=_auth_transport(ids[:3], [], [], groups_status=500), **k))
    _as(TEACHER_A)
    assert client.get(f"{API}/exams", params={"page": 1, "size": 50}).status_code == 503


def test_a_substitute_sees_only_the_exam_of_the_lesson_he_substitutes(client, db, two_teachers, monkeypatch):
    lesson = ("lesson-1", "2026-11-03")
    db.add_all([
        _row("EX_SUB_OK", created_by=TEACHER_B, group_id=GROUP_B, n=2000, lesson=lesson, status="lobby"),
        _row("EX_SUB_OTHER", created_by=TEACHER_B, group_id=GROUP_B, n=2001, lesson=("lesson-2", "2026-11-05"), status="lobby"),
    ])
    db.commit()

    async def substitute(actor):
        return {GROUP_B: {lesson}}

    monkeypatch.setattr(core_auth, "assigned_substitution_lessons", substitute)
    _as(TEACHER_A)
    ids, total = _all_pages(client, size=100)
    assert "EX_SUB_OK" in ids and "EX_SUB_OTHER" not in ids
    assert total == 76
    assert not [i for i in ids if i.startswith("EXB_")]


def test_a_vanished_group_hides_its_exams_and_nothing_else(client, db, two_teachers):
    db.add(_row("EX_GONE", created_by=TEACHER_A, group_id="00000000-0000-0000-0000-00000000dead", n=3000))
    db.commit()
    _as(TEACHER_A)
    ids, total = _all_pages(client, size=100)
    assert total == 75 and "EX_GONE" not in ids


def test_pinned_first_then_newest_and_the_order_is_total(client, db, two_teachers):
    for exam_id in ("EXA_000", "EXA_001"):
        db.get(GeneratedExam, exam_id).is_pinned = True
    for exam_id in ("EXA_010", "EXA_011"):          # the same instant: the tie must not shuffle between pages
        db.get(GeneratedExam, exam_id).created_at = BASE + timedelta(minutes=500)
    db.commit()
    _as(TEACHER_A)
    ids, _ = _all_pages(client, size=7)
    assert set(ids[:2]) == {"EXA_000", "EXA_001"}
    assert len(ids) == 75 and len(set(ids)) == 75
    assert ids.index("EXA_011") < ids.index("EXA_010")             # id desc breaks the tie, the same on every page


def test_the_card_carries_its_threshold(client, db, two_teachers):
    db.get(GeneratedExam, "EXA_005").pass_threshold_pct = 85
    db.commit()
    _as(TEACHER_A)
    items = client.get(f"{API}/exams", params={"page": 1, "size": 100}).json()["items"]
    assert {i["exam_id"]: i["pass_threshold_pct"] for i in items}["EXA_005"] == 85
    assert {i["pass_threshold_pct"] for i in items if i["exam_id"] != "EXA_005"} == {60}


# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# 5. EX-28: content-service down is 503 + Retry-After
# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _upstream_status(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://content/api/v1/content/textbooks/x/toc")
    return httpx.HTTPStatusError("upstream", request=request, response=httpx.Response(status_code, request=request))


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("content-service restarts"),
    httpx.ReadTimeout("slow"),
    _upstream_status(500),
    _upstream_status(502),
])
def test_content_service_failure_is_503_with_retry_after(client, failure):
    repo = MagicMock()
    repo.get_toc.side_effect = failure
    with patch("src.api.router.TextbookRepository", return_value=repo):
        response = client.get(f"{API}/textbooks/x/toc/tree")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert "Internal Server Error" not in response.text


@pytest.mark.parametrize("code", [400, 401, 403, 422])
def test_a_refusal_of_content_service_is_not_a_try_again(client, code):
    """R3 #9: a wrong service token or a refused request is not transient - no «повторите», no Retry-After, a plain 502."""
    repo = MagicMock()
    repo.get_toc.side_effect = _upstream_status(code)
    with patch("src.api.router.TextbookRepository", return_value=repo):
        response = client.get(f"{API}/textbooks/x/toc/tree")
    assert response.status_code == 502
    assert "Retry-After" not in response.headers
    assert "Повторите" not in response.text and "минуту" not in response.text


def test_a_generation_with_content_service_down_is_503(client):
    repo = MagicMock()
    repo.get_tasks_by_skills.side_effect = httpx.ConnectError("down")
    with patch("src.api.router.TaskRepository", return_value=repo):
        response = client.post(f"{API}/exams/generate", json={"title": "Контрольная", "skill_ids": ["SK_001"], "tasks_count": 5})
    assert response.status_code == 503 and response.headers["Retry-After"] == "30"


def test_a_missing_catalogue_entry_stays_a_404(client):
    repo = MagicMock()
    repo.get_toc.side_effect = _upstream_status(404)
    with patch("src.api.router.TextbookRepository", return_value=repo):
        assert client.get(f"{API}/textbooks/x/toc/tree").status_code == 404


# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# 6. EX-45: one client, not one TCP connection per call
# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_pooled_client_is_one_per_loop_and_timeout_and_is_not_closed_by_the_block():
    from src.core.http_pool import close_pools, pooled_client

    async def scenario():
        seen = []
        for _ in range(3):
            async with pooled_client(timeout=5.0) as client:
                seen.append(client)
        async with pooled_client(timeout=12.0) as other:
            seen.append(other)
        assert seen[0] is seen[1] is seen[2]
        assert other is not seen[0]
        assert not seen[0].is_closed
        await close_pools()
        assert seen[0].is_closed
        async with pooled_client(timeout=5.0) as fresh:      # after shutdown a new one is made, not a dead one reused
            assert fresh is not seen[0] and not fresh.is_closed
        await close_pools()
        return seen[0]

    first = asyncio.run(scenario())
    second_loop = asyncio.run(scenario())
    assert first is not second_loop                          # a client never travels to another event loop


def test_the_shared_client_keeps_no_cookies_between_calls():
    """R3 #2: a Set-Cookie answered to one person must not ride on the next person's request (the pool is shared by everybody)."""
    from src.core.http_pool import close_pools, pooled_client

    sent: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("cookie"))
        return httpx.Response(200, headers={"set-cookie": "algo_access=USER_A_TOKEN; Path=/"}, json={})

    real = httpx.AsyncClient

    async def scenario():
        async with pooled_client(timeout=5.0) as client:
            await client.get("http://auth.internal/api/v1/auth/me", headers={"authorization": "Bearer A"})
            await client.get("http://auth.internal/api/v1/auth/me", headers={"authorization": "Bearer B"})
            assert not list(client.cookies.jar)
        await close_pools()

    with patch.object(httpx, "AsyncClient", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k)):
        asyncio.run(scenario())
    assert sent == [None, None]


def test_staff_calls_to_auth_share_one_connection_pool(monkeypatch):
    built = []
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        client = real(*args, transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"id": TEACHER_A, "teacher_id": TEACHER_A, "name": "8А"})), **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(core_auth.httpx, "AsyncClient", factory)
    from src.core.http_pool import close_pools

    async def scenario():
        actor = ExamActor(user_id=TEACHER_A, org_id=TEST_ORG_ID, role="teacher", auth_headers={})
        for _ in range(4):
            await core_auth.require_group_access(actor, GROUP_A)
        await close_pools()

    asyncio.run(scenario())
    assert len(built) == 1                                   # four lookups, ONE client (it used to be four)


# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# 7. EX-44: one rule for «can a student enter now», one parser of started_at
# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_both_public_entry_checks_answer_410_for_an_exam_not_tied_to_a_lesson(db):
    from src.api import router

    unbound = _exam(db, status="lobby")
    unbound.scheduled_lesson_id = None
    unbound.access_code = "UNBOUND1"
    db.commit()
    for call in (lambda: router._public_exam_precheck(db, "UNBOUND1"), lambda: router._entry_gate(db, unbound.id)):
        with pytest.raises(HTTPException) as caught:
            call()
        assert caught.value.status_code == 410 and "не назначен" in caught.value.detail


def test_both_public_entry_checks_answer_410_for_a_finished_exam(db):
    from src.api import router

    done = _exam(db, status="closed")
    done.access_code = "DONE0001"
    db.commit()
    for call in (lambda: router._public_exam_precheck(db, "DONE0001"), lambda: router._entry_gate(db, done.id)):
        with pytest.raises(HTTPException) as caught:
            call()
        assert caught.value.status_code == 410 and "завершён" in caught.value.detail


def test_started_at_is_read_by_one_parser():
    from src.api.router import _settings_started_at

    class Row:
        id = "E"
        settings = {"started_at": "2026-10-30T10:00:00"}

    assert _settings_started_at(Row) == datetime(2026, 10, 30, 10, 0, 0)
    Row.settings = {"started_at": "2026-10-30T10:00:00+05:00"}
    assert _settings_started_at(Row) == datetime(2026, 10, 30, 5, 0, 0)            # naive UTC, like every other column
    for broken in ({}, {"started_at": ""}, {"started_at": "not a date"}, {"started_at": 5}):
        Row.settings = broken
        assert _settings_started_at(Row) is None


# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# 8. migration 017 (SQLite-safe parts; the PostgreSQL run is tests/test_wb_p1_migration_017.py)
# ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_model_declares_the_column_with_default_60_and_the_check():
    column = GeneratedExam.__table__.c.pass_threshold_pct
    assert column.nullable is False and column.default.arg == 60 and str(column.server_default.arg) == "60"
    assert any("pass_threshold_pct" in str(getattr(c, "sqltext", "")) for c in GeneratedExam.__table__.constraints)
