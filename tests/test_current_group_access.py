"""An exam author cannot bypass current classroom ownership after transfer."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.core import auth
from src.core.auth import ExamActor
from src.models.exam_models import GeneratedExam


def test_assigned_exam_creator_must_pass_current_group_access(monkeypatch):
    actor=ExamActor(user_id='former-teacher',org_id='org',role='teacher',auth_headers={})
    exam=GeneratedExam(id='assigned',org_id='org',created_by=actor.user_id,group_id='group')
    guard=AsyncMock(side_effect=HTTPException(403,'Group has another teacher'))
    monkeypatch.setattr(auth,'require_group_access',guard)
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.require_exam_access(exam,actor))
    assert error.value.status_code==403
    guard.assert_awaited_once_with(actor,'group')


def test_unassigned_library_template_remains_accessible_to_its_creator(monkeypatch):
    actor=ExamActor(user_id='teacher',org_id='org',role='teacher',auth_headers={})
    guard=AsyncMock()
    monkeypatch.setattr(auth,'require_group_access',guard)
    asyncio.run(auth.require_exam_access(GeneratedExam(org_id='org',created_by=actor.user_id),actor))
    guard.assert_not_awaited()


def test_unassigned_template_is_hidden_from_another_teacher():
    actor=ExamActor(user_id='teacher-2',org_id='org',role='teacher',auth_headers={})
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.require_exam_access(GeneratedExam(org_id='org',created_by='teacher-1'),actor))
    assert error.value.status_code==403


@pytest.mark.parametrize('role',['owner','admin'])
def test_administrative_observer_can_read_exam_in_own_org(role,monkeypatch):
    actor=ExamActor(user_id='observer',org_id='org',role=role,auth_headers={})
    guard=AsyncMock()
    monkeypatch.setattr(auth,'require_group_access',guard)
    asyncio.run(auth.require_exam_access(GeneratedExam(org_id='org',group_id='group'),actor))
    guard.assert_not_awaited()
