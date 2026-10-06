"""Live dashboard: a teacher's own grouped exams share one cached group lookup."""
import asyncio
from unittest.mock import AsyncMock

from src.api import router
from src.core import auth
from tests.conftest import TEST_GROUP, TEST_GROUP_ID, make_actor, make_exam_in_db


def _own_exams(db, n):
    exams = []
    for i in range(n):
        exam = make_exam_in_db(db)
        exam.id = f"EXAM_OWN_{i}"
        db.commit()
        exams.append(exam)
    return exams


def test_own_grouped_exams_do_not_trigger_per_exam_auth_call(db, monkeypatch):
    # Reuse the fixture-built exam, then clone it into 3 own exams of one group.
    first = make_exam_in_db(db)
    db.delete(first)
    db.commit()
    _own_exams(db, 3)

    actor = make_actor("teacher")
    assert not actor.is_org_admin
    guard = AsyncMock(return_value=dict(TEST_GROUP))
    monkeypatch.setattr(auth, "require_group_access", guard)

    result = asyncio.run(router.get_live_exam_dashboard_summary(db=db, actor=actor))

    assert len(result) == 3  # own exams still visible
    assert guard.await_count == 1  # one lookup for the group, not one per exam
    guard.assert_awaited_with(actor, TEST_GROUP_ID)


def test_own_exam_still_hidden_when_group_access_is_lost(db, monkeypatch):
    from fastapi import HTTPException

    first = make_exam_in_db(db)
    db.delete(first)
    db.commit()
    _own_exams(db, 2)

    actor = make_actor("teacher")
    guard = AsyncMock(side_effect=HTTPException(403, "Group has another teacher"))
    monkeypatch.setattr(auth, "require_group_access", guard)

    result = asyncio.run(router.get_live_exam_dashboard_summary(db=db, actor=actor))

    assert result == []  # authorization semantics unchanged: author loses access
    assert guard.await_count == 1  # negative result cached per group too
