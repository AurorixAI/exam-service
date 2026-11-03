"""Wave E, E-D (EX-43 / РАСХ-13): the state of the bound lesson gets ONE answer wherever somebody wants the exam now.  A student of a FINISHED lesson was told
«Экзамен станет доступен после начала…» (a promise that never comes true), the teacher of a cancelled or finished one «Сначала начните назначенный урок»; the teacher's
start also had its own copy of the attendance request.  Now: ``_lesson_state_refusal`` + one ``_lesson_attendance_status``."""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from src.api import router
from src.core.auth import ExamActor
from src.models.exam_models import GeneratedExam

DAY = date(2026, 11, 4)
WAIT_STUDENT = "Экзамен станет доступен после начала урока преподавателем"
WAIT_TEACHER = "Сначала начните назначенный урок, затем запускайте экзамен"
CANCELLED = "Назначенный урок отменён. Экзамен недоступен."
COMPLETED = "Экзамен этого урока завершён."


def _exam():
    return GeneratedExam(id="E1", org_id="org-1", title="Контрольная", status="lobby", delivery_mode="online", group_id="g-1", group_name="9А",
                         scheduled_lesson_id="lesson-1", scheduled_lesson_date=DAY.isoformat(), access_code="CODE", variants=[{"variant_number": 1, "tasks": []}], settings={})


class FakeAuth:
    """auth-service for one lesson: the attendance status of the occurrence (None = never started), both the teacher's and the internal endpoint."""

    def __init__(self, status):
        self.status, self.paths = status, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path.endswith("/schedule/occurrences"):
            return httpx.Response(200, json=[{"id": "lesson-1", "groupId": "g-1", "occurrenceDate": DAY.isoformat(), "startTime": "10:00", "endTime": "11:00"}])
        if "/internal/org/" in request.url.path:
            return httpx.Response(200, json={"status": self.status})
        statuses = {f"{DAY.isoformat()}_lesson-1": self.status} if self.status else {}
        return httpx.Response(200, json={"statuses": statuses})


@pytest.fixture
def world(monkeypatch):
    def install(status):
        spec = FakeAuth(status)
        real = httpx.AsyncClient
        monkeypatch.setattr(router.httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(spec.handler)}))
        monkeypatch.setattr(router, "get_settings", lambda: SimpleNamespace(internal_service_token="t", auth_service_url="http://auth", app_env="testing"))

        async def group_access(actor, group_id):
            return {"id": group_id, "name": "9А", "class_level": 9}

        monkeypatch.setattr(router, "require_group_access", group_access)
        monkeypatch.setattr(router, "_business_today", lambda: DAY)
        return spec

    return install


def run(coro):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(coro)
    return caught.value


def lobby(exam=None):
    return router._require_public_lobby_available(exam or _exam())


def start(exam=None):
    return router._require_lesson_in_progress(exam or _exam(), ExamActor(user_id="t-1", org_id="org-1", role="teacher", auth_headers={}))


@pytest.mark.parametrize("status,expected", [("in_progress", None), ("cancelled", (410, CANCELLED)), ("completed", (410, COMPLETED)), (None, (409, "X")), ("planned", (409, "X"))])
def test_the_rule(status, expected):
    got = router._lesson_state_refusal(status, not_started="X")
    assert (got is None) if expected is None else (got.status_code, got.detail) == expected


@pytest.mark.parametrize("status,expected", [("completed", (410, COMPLETED)), ("cancelled", (410, CANCELLED)), (None, (409, WAIT_STUDENT)), ("planned", (409, WAIT_STUDENT))])
def test_the_public_lobby_answers_by_the_state_of_the_lesson(world, status, expected):
    world(status)
    err = run(lobby())
    assert (err.status_code, err.detail) == expected          # on the old code a finished lesson was 409 «станет доступен»


def test_the_public_lobby_is_open_while_the_lesson_runs(world):
    world("in_progress")
    assert asyncio.run(lobby()) is None


@pytest.mark.parametrize("status,expected", [("completed", (410, COMPLETED)), ("cancelled", (410, CANCELLED)), (None, (409, WAIT_TEACHER))])
def test_the_teachers_start_answers_by_the_state_of_the_lesson(world, status, expected):
    world(status)
    err = run(start())
    assert (err.status_code, err.detail) == expected          # on the old code a cancelled / finished lesson: «сначала начните урок»


def test_the_teachers_start_runs_while_the_lesson_runs_and_reads_the_attendance_through_the_one_helper(world, monkeypatch):
    world("in_progress")
    calls = []
    real = router._lesson_attendance_status

    async def spy(actor, **kw):
        calls.append(kw)
        return await real(actor, **kw)

    monkeypatch.setattr(router, "_lesson_attendance_status", spy)
    assert asyncio.run(start()) is None
    assert calls == [{"group_id": "g-1", "lesson_id": "lesson-1", "lesson_date": DAY.isoformat()}]


def test_an_attendance_answer_that_is_not_200_is_503_for_the_start(world, monkeypatch):
    world("in_progress")

    original = FakeAuth.handler

    def broken(self, request):
        return httpx.Response(500) if request.url.path.endswith("/attendance") else original(self, request)

    monkeypatch.setattr(FakeAuth, "handler", broken)
    err = run(start())
    assert err.status_code == 503 and err.detail == "Не удалось проверить статус урока"
