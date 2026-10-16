"""Regression tests for the isolated print-exam lifecycle."""
from __future__ import annotations

import asyncio

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool

import src.api.router as router
from src.api.router import AssignExamRequest
from src.core.auth import ExamActor
from src.models.exam_models import GeneratedExam

from tests.conftest import TEST_GROUP_ID, TEST_ORG_ID, TEST_USER_ID


def test_legacy_assignment_can_never_open_a_lobby_for_print_delivery(monkeypatch) -> None:
    """Paper exams may only be bound by the immutable print-package flow.

    This protects against a future change where ``publish=False`` accidentally
    bypasses the normal online-lobby guard and gives a paper exam an access
    code.  The handler must reject before it asks the group service anything.
    """
    exam = GeneratedExam(
        id="print-no-lobby",
        title="Печатная контрольная",
        delivery_mode="print",
        status="draft",
    )

    def get_print_exam(*_args, **_kwargs):
        return exam

    monkeypatch.setattr(router, "_get_exam_for_teacher_operator", get_print_exam)
    actor = ExamActor(
        user_id="teacher-1",
        org_id="org-1",
        role="teacher",
        auth_headers={},
    )

    async def attempt_assignment() -> None:
        with pytest.raises(HTTPException) as error:
            await run_in_threadpool(
                router.assign_exam,
                exam.id,
                AssignExamRequest(group_id="any-group", publish=False),
                MagicMock(),
                actor,
            )

        assert error.value.status_code == 422
        assert "печатного комплекта" in str(error.value.detail)
        assert exam.status == "draft"
        assert exam.access_code is None

    asyncio.run(attempt_assignment())


class TestCloneAndRenameLifecycle:
    """Regression coverage for the reusable-exam lifecycle boundary."""

    def test_rename_is_allowed_only_for_an_editable_draft(self, client, db) -> None:
        exam = GeneratedExam(
            id="rename-draft",
            title="Исходное название",
            status="variants_ready",
            org_id=TEST_ORG_ID,
            created_by=TEST_USER_ID,
            variants=[{"variant_number": 1, "tasks": []}],
            settings={},
        )
        db.add(exam)
        db.commit()

        response = client.patch(f"/api/v1/exams/{exam.id}/title", json={"title": "  Новое название  "})

        assert response.status_code == 200
        assert response.json()["title"] == "Новое название"
        db.refresh(exam)
        assert exam.title == "Новое название"

        exam.status = "closed"
        db.commit()
        blocked = client.patch(f"/api/v1/exams/{exam.id}/title", json={"title": "Другое название"})
        assert blocked.status_code == 409
        assert "создайте копию" in blocked.json()["detail"].lower()

    def test_clone_resets_run_context_and_numbers_default_titles(self, client, db) -> None:
        source = GeneratedExam(
            id="clone-source",
            title="Контрольная по функциям",
            status="closed",
            org_id=TEST_ORG_ID,
            created_by=TEST_USER_ID,
            delivery_mode="print",
            group_id=TEST_GROUP_ID,
            group_name="Старая группа",
            scheduled_lesson_id="lesson-old",
            scheduled_lesson_date="2026-08-30",
            access_code="EXOLD",
            variants=[{"variant_number": 1, "tasks": [{"task_id": "T1"}]}],
            settings={"print_layout": {"schema_version": 1}},
            skills_covered=["SK-1"],
        )
        db.add(source)
        db.commit()

        first = client.post(f"/api/v1/exams/{source.id}/clone", json={})
        second = client.post(f"/api/v1/exams/{source.id}/clone", json={})

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["title"] == "Контрольная по функциям — копия"
        assert second.json()["title"] == "Контрольная по функциям — копия 2"

        copied = db.query(GeneratedExam).filter(GeneratedExam.id == first.json()["exam_id"]).one()
        assert copied.status == "variants_ready"
        assert copied.source_exam_id == source.id
        assert copied.group_id is None
        assert copied.scheduled_lesson_id is None
        assert copied.scheduled_lesson_date is None
        assert copied.access_code is None
        assert copied.variants == source.variants
        assert copied.variants is not source.variants

    def test_archived_exam_cannot_be_cloned_until_restored(self, client, db) -> None:
        exam = GeneratedExam(
            id="clone-archived",
            title="Архивный экзамен",
            status="archived",
            org_id=TEST_ORG_ID,
            created_by=TEST_USER_ID,
            variants=[],
            settings={},
        )
        db.add(exam)
        db.commit()

        response = client.post(f"/api/v1/exams/{exam.id}/clone", json={})
        assert response.status_code == 409
        assert "сначала восстановите" in response.json()["detail"].lower()
