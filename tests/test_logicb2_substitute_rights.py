"""LOGIC-B2 D-1 (lead's ruling 21.10): a substitute RUNS the exam of the lesson he substitutes in and nothing else.

Capability, not ad-hoc checks: ``require_exam_access`` returns an ``ExamAccess`` (``kind == "substitute"``) and a substitute passes only the operations of
``SUBSTITUTE_EXAM_OPS``; every other route (named or not) answers 403.  The group's teacher and the managers are not affected.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.api import router as router_module
from src.core import auth as core_auth
from tests.conftest import TEST_GROUP_ID, TEST_LESSON_ID, make_actor, make_exam_in_db, make_session_in_db

API = "/api/v1"


@pytest.fixture
def lesson_exam(client, db, monkeypatch):
    """An exam bound to ``(LSN_TEST01, today)`` in a group whose page is closed (403) to the actor: he can only be its substitute."""
    exam = make_exam_in_db(db, status="lobby")
    exam.access_code = "SUBCODE1"
    db.commit()

    async def refuse(actor, group_id):
        raise HTTPException(403, "Нет доступа к этой группе")

    access = {"id": TEST_GROUP_ID, "org_id": "ORG_TEST01", "name": "8А", "class_level": 8, "substitution": True,
              "lessons": [{"lessonId": TEST_LESSON_ID, "lessonDate": exam.scheduled_lesson_date}]}
    monkeypatch.setattr(core_auth, "require_group_access", refuse)
    monkeypatch.setattr(router_module, "require_group_access", refuse)
    monkeypatch.setattr(core_auth, "lesson_access", AsyncMock(return_value=access))
    return exam


def _calls(exam, session):
    d = exam.scheduled_lesson_date
    return {
        # allowed: the operations that run the lesson
        "get": ("get", f"/exams/{exam.id}", None),
        "lobby-status": ("get", f"/exams/{exam.id}/lobby-status", None),
        "open-lobby": ("post", f"/exams/{exam.id}/open-lobby", {"lesson_id": TEST_LESSON_ID, "lesson_date": d}),
        "start-live": ("post", f"/exams/{exam.id}/start-live", None),
        "close": ("post", f"/exams/{exam.id}/close", None),
        "sessions": ("get", f"/exams/{exam.id}/sessions", None),
        "kick": ("delete", f"/exams/{exam.id}/sessions/{session.id}", None),
        "cancel-delivery": ("post", f"/exams/{exam.id}/delivery/cancel", {"reason": "урок отменён"}),
        "report": ("get", f"/exams/{exam.id}/report", None),
    }


def _refused_calls(exam):
    d = exam.scheduled_lesson_date
    return {
        "delete / archive": ("delete", f"/exams/{exam.id}", None),
        "delete permanent": ("delete", f"/exams/{exam.id}?permanent=true", None),
        "restore": ("post", f"/exams/{exam.id}/restore", None),
        "clone": ("post", f"/exams/{exam.id}/clone", {}),
        "rename": ("patch", f"/exams/{exam.id}/title", {"title": "Другое"}),
        "pin": ("post", f"/exams/{exam.id}/pin", None),
        "draft": ("get", f"/exams/{exam.id}/draft", None),
        "draft alternatives": ("get", f"/exams/{exam.id}/draft/alternatives?variant_number=1&task_id=TASK_001", None),
        "replace-task": ("post", f"/exams/{exam.id}/draft/replace-task", {"variant_number": 1, "task_id": "TASK_001", "old_task_id": "TASK_001", "new_task_id": "TASK_002"}),
        "publish": ("post", f"/exams/{exam.id}/publish", {"lesson_id": TEST_LESSON_ID, "lesson_date": d}),
        "assign": ("post", f"/exams/{exam.id}/assign", {"group_id": TEST_GROUP_ID, "lesson_id": TEST_LESSON_ID, "lesson_date": d}),
        "generate-variants": ("post", f"/exams/{exam.id}/generate-variants?variants_count=3", None),
        "entry-limits reset": ("post", f"/exams/{exam.id}/entry-limits/reset", None),
        "print-layout": ("put", f"/exams/{exam.id}/print-layout", {"layout": {}}),
        "print-package prepare": ("post", f"/exams/{exam.id}/print-package/prepare", {"group_id": TEST_GROUP_ID, "lesson_id": TEST_LESSON_ID, "lesson_date": d}),
        "print-package start": ("post", f"/exams/{exam.id}/print-package/start", None),
        "print-package status": ("get", f"/exams/{exam.id}/print-package", None),
        "print-preview pdf": ("get", f"/exams/{exam.id}/print-preview/teacher-key/1.pdf", None),
        "student pdf": ("get", f"/exams/{exam.id}/print-package/student/1.pdf", None),
        "teacher-key pdf": ("get", f"/exams/{exam.id}/print-package/teacher-key/1.pdf", None),
        "analytics": ("get", f"/exams/{exam.id}/analytics", None),
        "report generate": ("post", f"/exams/{exam.id}/report", None),
        "admin view": ("get", f"/admin/exams/{exam.id}/view", None),
    }


def _send(client, call):
    method, path, body = call
    return getattr(client, method)(f"{API}{path}", **({"json": body} if body is not None else {}))


def test_the_allow_list_is_exactly_the_lessons_operations():
    assert core_auth.SUBSTITUTE_EXAM_OPS == {"get", "open-lobby", "lobby-status", "start-live", "close", "sessions", "kick", "cancel-delivery", "report"}
    full, sub = core_auth.ExamAccess("full"), core_auth.ExamAccess("substitute")
    assert full.allows(None) and full.allows("anything") and not full.is_substitute
    assert sub.is_substitute and sub.allows("close") and not sub.allows(None) and not sub.allows("delete")      # default deny


def test_require_exam_access_returns_the_kind(lesson_exam, monkeypatch):
    """A substitute is told apart from the group's teacher in the decision itself."""
    teacher = make_actor("teacher")
    assert asyncio.run(core_auth.require_exam_access(lesson_exam, teacher, "get")).kind == "substitute"
    with pytest.raises(HTTPException) as caught:
        asyncio.run(core_auth.require_exam_access(lesson_exam, teacher, "delete"))
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException) as caught:                                                        # an operation that names nothing: closed
        asyncio.run(core_auth.require_exam_access(lesson_exam, teacher))
    assert caught.value.status_code == 403

    async def group_ok(actor, group_id):
        return {"id": group_id}

    monkeypatch.setattr(core_auth, "require_group_access", group_ok)
    assert asyncio.run(core_auth.require_exam_access(lesson_exam, teacher, "delete")).kind == "full"
    assert asyncio.run(core_auth.require_exam_access(lesson_exam, make_actor("admin"), "delete")).kind == "full"


@pytest.mark.parametrize("name", sorted(core_auth.SUBSTITUTE_EXAM_OPS))
def test_a_substitute_runs_the_lesson_exam(client, db, lesson_exam, name):
    """[allowed] each operation of the allow-list reaches the handler (anything but the access refusal)."""
    session = make_session_in_db(db, lesson_exam, status="waiting")
    resp = _send(client, _calls(lesson_exam, session)[name])
    assert resp.status_code != 403, (name, resp.status_code, resp.text[:200])
    assert resp.status_code < 500, (name, resp.status_code, resp.text[:200])


@pytest.mark.parametrize("name", list(_refused_calls(type("E", (), {"id": "X", "scheduled_lesson_date": "2026-10-26"})())))
def test_a_substitute_is_refused_everything_else(client, db, lesson_exam, name):
    """[refused] each family outside the allow-list answers 403 and changes nothing."""
    resp = _send(client, _refused_calls(lesson_exam)[name])
    assert resp.status_code == 403, (name, resp.status_code, resp.text[:200])
    assert "Заменяющий" in resp.json()["detail"] or name == "admin view", resp.text[:200]
    db.refresh(lesson_exam)
    assert lesson_exam.status == "lobby" and lesson_exam.title == "Тестовый экзамен"


def test_the_exam_of_another_lesson_is_closed_even_for_the_allowed_operations(client, db, lesson_exam):
    lesson_exam.scheduled_lesson_id = "LSN_OTHER"
    db.commit()
    for name, call in (("get", ("get", f"/exams/{lesson_exam.id}", None)), ("close", ("post", f"/exams/{lesson_exam.id}/close", None))):
        assert _send(client, call).status_code == 403, name


def test_the_group_teacher_and_the_manager_keep_their_rights(client, db, lesson_exam, monkeypatch):
    """[unaffected] the group's own teacher (group access granted) passes both the allowed and the refused families; so does an admin."""
    async def group_ok(actor, group_id):
        return {"id": group_id, "name": "8А", "class_level": 8}

    monkeypatch.setattr(core_auth, "require_group_access", group_ok)
    monkeypatch.setattr(router_module, "require_group_access", group_ok)
    lesson_access = AsyncMock(side_effect=AssertionError("the group's teacher never asks lesson-access"))
    monkeypatch.setattr(core_auth, "lesson_access", lesson_access)
    assert client.get(f"{API}/exams/{lesson_exam.id}/draft").status_code != 403
    assert client.post(f"{API}/exams/{lesson_exam.id}/pin").status_code != 403
    assert client.get(f"{API}/exams/{lesson_exam.id}/analytics").status_code != 403
    assert client.get(f"{API}/exams/{lesson_exam.id}").status_code == 200
    lesson_access.assert_not_awaited()
    from src.main import app
    from src.core.auth import require_exam_staff
    app.dependency_overrides[require_exam_staff] = lambda: make_actor("admin")
    assert client.get(f"{API}/admin/exams/{lesson_exam.id}/view").status_code != 403
    assert client.get(f"{API}/exams/{lesson_exam.id}/analytics").status_code != 403


def test_a_list_still_shows_the_substitute_only_his_lesson_exam(db, lesson_exam):
    from fastapi.concurrency import run_in_threadpool

    decisions = asyncio.run(run_in_threadpool(router_module._exam_access_decisions, db, [lesson_exam], make_actor("teacher")))
    assert decisions == {lesson_exam.id: True}


def test_open_lobby_of_a_substitute_is_for_the_lesson_the_exam_is_bound_to(monkeypatch):
    """[open-lobby] the real lookup lets the substitute through for the exam's own (future) lesson only; a different lesson of the group stays 403."""
    from tests.test_logicb2_substitute_access import ACCESS, GROUP, _refuse

    _refuse(monkeypatch)
    monkeypatch.setattr(core_auth, "lesson_access", AsyncMock(return_value=dict(ACCESS)))

    class Resp:
        status_code = 200

        def __init__(self, url):
            self.url = url

        def json(self):
            if self.url.endswith("/attendance"):
                return {"statuses": {}}
            return [{"id": "lesson-l1", "groupId": GROUP, "occurrenceDate": "2099-10-26", "startTime": "10:00", "endTime": "11:30"}]

    class C:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None): return Resp(url)

    monkeypatch.setattr(router_module.httpx, "AsyncClient", C)
    access = {**ACCESS, "lessons": [{"lessonId": "lesson-l1", "lessonDate": "2099-10-26"}, {"lessonId": "lesson-l2", "lessonDate": "2099-10-28"}]}
    monkeypatch.setattr(core_auth, "lesson_access", AsyncMock(return_value=access))
    actor = make_actor("teacher")
    group, _ = asyncio.run(router_module._resolve_lesson_occurrence(
        actor, group_id=GROUP, lesson_id="lesson-l1", lesson_date="2099-10-26", substitute_bound_lesson=("lesson-l1", "2099-10-26")))
    assert group["id"] == GROUP
    for kwargs in ({"substitute_bound_lesson": ("lesson-l1", "2099-10-26"), "lesson_id": "lesson-l2", "lesson_date": "2099-10-28"},   # moving the exam to another lesson
                   {"lesson_id": "lesson-l1", "lesson_date": "2099-10-26"}):                                                          # binding with no bound lesson
        with pytest.raises(HTTPException) as caught:
            asyncio.run(router_module._resolve_lesson_occurrence(actor, group_id=GROUP, **kwargs))
        assert caught.value.status_code == 403


# ── M4: the list does not call auth-service once per refused group ────────────────────────────────────────────────────────────
def _count_lookups(monkeypatch, *, forbidden):
    calls = {"group": 0, "lesson_access": 0, "peak": 0, "now": 0}

    async def group(actor, group_id):
        calls["group"] += 1
        raise HTTPException(403 if group_id in forbidden else 404, "x")

    async def substitute_lessons(actor, group_id):
        calls["lesson_access"] += 1
        calls["now"] += 1
        calls["peak"] = max(calls["peak"], calls["now"])
        await asyncio.sleep(0.02)
        calls["now"] -= 1
        return {("lsn", "2026-10-26")}

    monkeypatch.setattr(core_auth, "require_group_access", group)
    monkeypatch.setattr(core_auth, "substitute_lessons", substitute_lessons)
    return calls


def _exams(n, *, bound):
    from src.models.exam_models import GeneratedExam

    return [GeneratedExam(id=f"E{i}", org_id="ORG_TEST01", group_id=f"00000000-0000-0000-0000-{i:012d}", created_by="other", title="x",
                          scheduled_lesson_id="lsn" if bound else None, scheduled_lesson_date="2026-10-26" if bound else None) for i in range(n)]


def test_no_substitution_lookup_when_no_exam_has_a_lesson_key(db, monkeypatch):
    from fastapi.concurrency import run_in_threadpool

    exams = _exams(6, bound=False)
    calls = _count_lookups(monkeypatch, forbidden={e.group_id for e in exams})
    decisions = asyncio.run(run_in_threadpool(router_module._exam_access_decisions, db, exams, make_actor("teacher")))
    assert calls["lesson_access"] == 0 and calls["group"] == 6
    assert not any(decisions.values())


def test_substitution_lookups_run_together_and_only_for_groups_with_a_bound_exam(db, monkeypatch):
    from fastapi.concurrency import run_in_threadpool

    bound, unbound = _exams(12, bound=True), _exams(4, bound=False)
    for i, e in enumerate(unbound):
        e.id, e.group_id = f"U{i}", f"11111111-0000-0000-0000-{i:012d}"
    exams = bound + unbound
    calls = _count_lookups(monkeypatch, forbidden={e.group_id for e in exams})
    decisions = asyncio.run(run_in_threadpool(router_module._exam_access_decisions, db, exams, make_actor("teacher")))
    assert calls["lesson_access"] == 12                                  # not the 4 groups without a lesson key; not a 404 group
    assert 1 < calls["peak"] <= 8                                        # concurrent, bounded by the semaphore
    assert all(decisions[e.id] for e in bound) and not any(decisions[e.id] for e in unbound)
