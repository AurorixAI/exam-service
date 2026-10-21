"""LOGIC-B2 D-1: a substitute teacher works the exam of the lesson he substitutes in - and nothing else of the group; another teacher gets 403."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool

from src.api import router as router_module
from src.core import auth as core_auth
from src.models.exam_models import GeneratedExam

from tests.conftest import make_actor

GROUP = "11111111-2222-3333-4444-555555555555"
ACCESS = {"id": GROUP, "org_id": "ORG_TEST01", "name": "9-А", "class_level": 9, "teacher_id": "someone-else", "substitution": True,
          "lessons": [{"lessonId": "lesson-l1", "lessonDate": "2026-10-26"}]}


def _exam(exam_id, lesson="lesson-l1", date="2026-10-26", group=GROUP):
    return GeneratedExam(id=exam_id, org_id="ORG_TEST01", group_id=group, created_by="the-main-teacher", title="x", scheduled_lesson_id=lesson, scheduled_lesson_date=date)


def _refuse(monkeypatch):
    async def lookup(actor, group_id):
        raise HTTPException(403, "Нет доступа к этой группе")          # the group page of auth-service is closed to a substitute

    monkeypatch.setattr(core_auth, "require_group_access", lookup)
    monkeypatch.setattr(router_module, "require_group_access", lookup)


def test_the_substitute_opens_the_exam_of_his_lesson_only(monkeypatch):
    """[D-1] The exam bound to the substituted lesson: allowed; an exam of another lesson of the same group, an unbound one: 403."""
    _refuse(monkeypatch)
    monkeypatch.setattr(core_auth, "lesson_access", AsyncMock(return_value=dict(ACCESS)))
    teacher = make_actor("teacher")
    asyncio.run(core_auth.require_exam_access(_exam("E1"), teacher))
    for other in (_exam("E2", lesson="lesson-l2", date="2026-10-28"), _exam("E3", date="2026-11-02"), _exam("E4", lesson=None, date=None)):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(core_auth.require_exam_access(other, teacher))
        assert caught.value.status_code == 403


def test_a_teacher_without_a_substitution_gets_403(monkeypatch):
    """[D-1] No substitution (lesson-access refuses or is unreachable): the original 403 stands; admins are not affected."""
    _refuse(monkeypatch)
    monkeypatch.setattr(core_auth, "lesson_access", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(core_auth.require_exam_access(_exam("E1"), make_actor("teacher")))
    assert caught.value.status_code == 403
    asyncio.run(core_auth.require_exam_access(_exam("E1"), make_actor("admin")))        # an admin never goes through the group lookup


def test_lesson_access_fails_closed(monkeypatch):
    """[D-1] ``lesson_access`` is a clear 200 for a substitution of this centre; a 403, a transport error, another centre, a non-substitution answer = ``None``."""
    class Resp:
        def __init__(self, code, body):
            self.status_code, self._body = code, body

        def json(self):
            return self._body

    def client_for(answer):
        class C:
            def __init__(self, *a, **kw): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, headers=None):
                assert url.endswith(f"/api/v1/auth/org/groups/{GROUP}/lesson-access")
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return C

    import httpx

    actor = make_actor("teacher")
    for answer, expected in ((Resp(200, dict(ACCESS)), True), (Resp(403, {}), False), (httpx.ConnectError("down"), False),
                             (Resp(200, {**ACCESS, "org_id": "OTHER"}), False), (Resp(200, {**ACCESS, "substitution": False}), False)):
        monkeypatch.setattr(core_auth.httpx, "AsyncClient", client_for(answer))
        got = asyncio.run(core_auth.lesson_access(actor, GROUP))
        assert (got is not None) is expected, answer
    assert asyncio.run(core_auth.lesson_access(actor, "GRP_not_a_uuid")) is None


def test_a_list_of_exams_shows_the_substitute_only_the_exam_of_his_lesson(db, monkeypatch):
    """[D-1] The library list: the exam of the substituted lesson is visible, the others of the group are not."""
    _refuse(monkeypatch)
    monkeypatch.setattr(core_auth, "lesson_access", AsyncMock(return_value=dict(ACCESS)))
    exams = [_exam("E1"), _exam("E2", lesson="lesson-l2", date="2026-10-28"), _exam("E3", date="2026-11-02")]
    decisions = asyncio.run(run_in_threadpool(router_module._exam_access_decisions, db, exams, make_actor("teacher")))
    assert decisions == {"E1": True, "E2": False, "E3": False}


def test_the_start_gate_of_a_lesson_exam_is_open_to_the_substitute_of_that_lesson(monkeypatch):
    """[D-1] ``_resolve_lesson_occurrence`` of the start gate (``require_current_or_future=False``) takes the group from ``lesson-access`` for the substituted lesson, and refuses another one."""
    _refuse(monkeypatch)
    monkeypatch.setattr(core_auth, "lesson_access", AsyncMock(return_value=dict(ACCESS)))

    class Resp:
        status_code = 200

        def json(self):
            return [{"id": "lesson-l1", "groupId": GROUP, "occurrenceDate": "2026-10-26", "startTime": "10:00", "endTime": "11:30"}]

    class C:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None): return Resp()

    monkeypatch.setattr(router_module.httpx, "AsyncClient", C)
    actor = make_actor("teacher")
    group, occurrence = asyncio.run(router_module._resolve_lesson_occurrence(actor, group_id=GROUP, lesson_id="lesson-l1", lesson_date="2026-10-26", require_current_or_future=False))
    assert group["id"] == GROUP and occurrence["id"] == "lesson-l1"
    with pytest.raises(HTTPException) as caught:
        asyncio.run(router_module._resolve_lesson_occurrence(actor, group_id=GROUP, lesson_id="lesson-l2", lesson_date="2026-10-28", require_current_or_future=False))
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException) as caught:                                          # binding a NEW exam to a lesson is not a substitute's act
        asyncio.run(router_module._resolve_lesson_occurrence(actor, group_id=GROUP, lesson_id="lesson-l1", lesson_date="2026-10-26", require_current_or_future=True))
    assert caught.value.status_code in (403, 409)
