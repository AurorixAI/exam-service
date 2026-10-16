"""Regression coverage for safe scheduled-exam delivery transitions."""
from __future__ import annotations

import asyncio

from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool

import src.api.router as router
from src.api.router import AssignExamRequest, CancelExamDeliveryRequest
from src.core.auth import ExamActor
from src.models.exam_models import GeneratedExam


def _actor() -> ExamActor:
    return ExamActor(user_id="teacher-1", org_id="org-1", role="teacher", auth_headers={})


def _bound_lobby() -> GeneratedExam:
    return GeneratedExam(
        id="delivery-lobby",
        title="Контрольная",
        status="lobby",
        delivery_mode="online",
        group_id="group-1",
        group_name="9А",
        scheduled_lesson_id="lesson-1",
        scheduled_lesson_date="2026-09-01",
        access_code="OLD-CODE",
        variants=[{"variant_number": 1, "tasks": []}],
        settings={},
    )


def test_public_lobby_rejects_future_and_past_lessons_before_network(monkeypatch) -> None:
    """A copied public URL never opens a waiting session outside the lesson."""
    exam = _bound_lobby()
    monkeypatch.setattr(router, "_business_today", lambda: date(2026, 9, 1))

    async def future() -> None:
        exam.scheduled_lesson_date = "2026-09-02"
        with pytest.raises(HTTPException) as error:
            await router._require_public_lobby_available(exam)
        assert error.value.status_code == 409
        assert "после начала" in str(error.value.detail).lower()

    async def past() -> None:
        exam.scheduled_lesson_date = "2026-08-31"
        with pytest.raises(HTTPException) as error:
            await router._require_public_lobby_available(exam)
        assert error.value.status_code == 410
        assert "уже прошло" in str(error.value.detail).lower()

    asyncio.run(future())
    asyncio.run(past())


def test_online_rebind_rotates_the_public_access_code(monkeypatch) -> None:
    """Moving an untouched lobby invalidates the previous QR/link."""
    exam = _bound_lobby()
    db = MagicMock()
    actor = _actor()

    def get_exam(*_args, **_kwargs):
        return exam

    async def group_access(*_args, **_kwargs):
        return {"id": "group-1", "name": "9А", "class_level": 9, "teacher_name": "Учитель"}

    async def resolve(*_args, **_kwargs):
        return await group_access(), {"id": "lesson-2"}

    monkeypatch.setattr(router, "_get_exam_for_teacher_operator", get_exam)
    monkeypatch.setattr(router, "require_group_access", group_access)
    monkeypatch.setattr(router, "_resolve_lesson_occurrence", resolve)
    monkeypatch.setattr(router, "_ensure_lesson_slot_is_available", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(router, "_has_any_sessions", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(router, "_generate_access_code", lambda: "NEW-CODE")

    async def move() -> None:
        # the teacher routes are plain ``def``: FastAPI runs them in a worker thread, so the test does the same
        result = await run_in_threadpool(
            router.assign_exam,
            exam.id,
            AssignExamRequest(
                group_id="group-1",
                publish=True,
                lesson_id="lesson-2",
                lesson_date="2026-09-03",
            ),
            db,
            actor,
        )
        assert result["access_code"] == "NEW-CODE"
        assert exam.access_code == "NEW-CODE"
        assert exam.scheduled_lesson_id == "lesson-2"
        assert exam.settings["lifecycle_events"][-1]["details"]["access_code_rotated"] is True

    asyncio.run(move())


def test_cancelling_an_unstarted_online_delivery_revokes_public_access(monkeypatch) -> None:
    """Cancellation returns a lobby to a reusable draft without deleting tasks."""
    exam = _bound_lobby()
    db = MagicMock()
    db.query.return_value.filter.return_value.with_for_update.return_value.populate_existing.return_value.first.return_value = exam

    def get_exam(*_args, **_kwargs):
        return exam

    monkeypatch.setattr(router, "_get_exam_for_teacher_operator", get_exam)
    monkeypatch.setattr(router, "_has_any_sessions", lambda *_args, **_kwargs: False)

    async def cancel() -> None:
        result = await run_in_threadpool(
            router.cancel_exam_delivery,
            exam.id,
            CancelExamDeliveryRequest(reason="Урок отменён преподавателем"),
            db,
            _actor(),
        )
        assert result["status"] == "variants_ready"
        assert result["requires_clone_for_print"] is False
        assert exam.access_code is None
        assert exam.scheduled_lesson_id is None
        assert exam.scheduled_lesson_date is None
        assert exam.variants == [{"variant_number": 1, "tasks": []}]

    asyncio.run(cancel())
