"""Wave E, E-D, decision 9: ``assign`` (publish) / ``publish`` / ``open-lobby`` bind an exam to a lesson through ONE gate - the same order of checks and the same
texts of refusal on the same input (the audit: the teacher read three wordings of one reason).

Order (rights -> the group under the lock -> the slot -> the state): the exam's own rights first (``_get_exam_for_teacher_operator``, then the rights on the target group
together with the lesson lookup, outside every lock); the cheap answers that need nothing but the request and the row (print exam, no group, no lesson, the state) before
any network; under the row lock the group is checked once more (the rights were decided for IT), then the slot, then the state.  ``COMPARE`` is the table the
three routes must agree on; set ``WE_D_PROBE=<file>`` to dump it instead of asserting (it was dumped from the code before the change to fill the report)."""
from __future__ import annotations

import json
import os

import pytest

from src.api import router
from src.models.exam_models import ExamSession, GeneratedExam
from tests.conftest import TEST_GROUP_ID, TEST_LESSON_ID, make_exam_in_db, make_task

API = "/api/v1"
ROUTES = ("assign", "publish", "open-lobby")
OTHER_GROUP = "99999999-2222-3333-4444-555555555555"

PRINT_TEXT = "Печатный экзамен не использует лобби. Подготовьте печатный комплект для занятия."
NO_GROUP_TEXT = "Нельзя открыть лобби для экзамена без группы. Сначала привяжите экзамен к группе."
STATE_TEXT = "Назначить или перенести экзамен можно только до его запуска"
ATTEMPTS_TEXT = "У экзамена уже есть попытки учеников. Создайте копию экзамена, чтобы провести его для другого занятия или группы"
GROUP_CHANGED_TEXT = "Группа экзамена изменилась, повторите"


def _exam(db, status="draft", exam_id="EXAM_G1", delivery="online", group=TEST_GROUP_ID, lesson=None):
    exam = make_exam_in_db(db, tasks=[make_task("T1", answer="3")], status=status)
    exam.id = exam_id
    exam.delivery_mode = delivery
    exam.group_id = group
    exam.scheduled_lesson_id, exam.scheduled_lesson_date = (lesson or (None, None))
    exam.access_code = None
    db.commit()
    return exam


def _call(client, route, exam, *, day, group_id=TEST_GROUP_ID):
    body = {"lesson_id": TEST_LESSON_ID, "lesson_date": day}
    if route == "assign":
        return client.post(f"{API}/exams/{exam.id}/assign", json={"group_id": group_id, "publish": True, **body})
    if route == "publish":
        return client.post(f"{API}/exams/{exam.id}/publish", json={"group_id": group_id, **body})
    return client.post(f"{API}/exams/{exam.id}/open-lobby", json=body)


def _day():
    from tests.conftest import business_today

    return business_today().isoformat()


def _session(db, exam):
    db.add(ExamSession(id="SES_G1", exam_id=exam.id, student_name="Иван", variant_number=1, answers={}, max_score=10, status="waiting"))
    db.commit()


def _outcome(response):
    return response.status_code, response.json().get("detail") if response.status_code != 200 else "ok"


def _twin_on_the_slot(db, day):
    twin = make_exam_in_db(db, tasks=[make_task("T9", answer="1")], status="lobby")
    twin.id = "EXAM_TWIN"
    twin.title = "Соседний экзамен"
    twin.scheduled_lesson_id, twin.scheduled_lesson_date, twin.access_code = TEST_LESSON_ID, day, "TWINCODE"
    db.commit()
    return twin


SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


@scenario
def print_exam(client, db, route, monkeypatch):
    return _call(client, route, _exam(db, delivery="print"), day=_day())


@scenario
def no_group(client, db, route, monkeypatch):
    exam = _exam(db, group=None)
    if route == "assign":
        pytest.skip("assign always names the group")
    return _call(client, route, exam, day=_day(), group_id=None)


@scenario
def has_attempts(client, db, route, monkeypatch):
    exam = _exam(db, status="lobby")
    _session(db, exam)
    return _call(client, route, exam, day=_day())


@scenario
def already_started(client, db, route, monkeypatch):
    return _call(client, route, _exam(db, status="active"), day=_day())


@scenario
def started_and_slot_busy(client, db, route, monkeypatch):
    exam = _exam(db, status="active")
    _twin_on_the_slot(db, _day())
    return _call(client, route, exam, day=_day())


@scenario
def slot_busy(client, db, route, monkeypatch):
    exam = _exam(db)
    _twin_on_the_slot(db, _day())
    return _call(client, route, exam, day=_day())


@scenario
def group_changed_before_the_lock(client, db, route, monkeypatch):
    exam = _exam(db)
    real = router._resolve_lesson_occurrence

    async def moved_meanwhile(actor, **kwargs):
        db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).update({"group_id": OTHER_GROUP})
        db.commit()
        return await real(actor, **kwargs)

    monkeypatch.setattr(router, "_resolve_lesson_occurrence", moved_meanwhile)
    return _call(client, route, exam, day=_day())


@scenario
def started_and_slot_busy_only_under_the_lock(client, db, route, monkeypatch):
    """The cheap pre-lock checks passed (draft, no attempts); a twin started the exam and another exam took the slot before the lock: the slot is named first."""
    exam = _exam(db)
    real = router._lock_exam_row

    def twins_got_there_first(session, row):
        locked = real(session, row)
        locked.status = "active"
        _twin_on_the_slot(session, _day())
        return locked

    monkeypatch.setattr(router, "_lock_exam_row", twins_got_there_first)
    return _call(client, route, exam, day=_day())


@scenario
def free(client, db, route, monkeypatch):
    return _call(client, route, _exam(db), day=_day())


# the verdicts of the new gate: one per scenario, the same for every route
COMPARE = {
    "print_exam": (409, PRINT_TEXT),
    "no_group": (400, NO_GROUP_TEXT),
    "has_attempts": (409, ATTEMPTS_TEXT),
    "already_started": (409, STATE_TEXT),
    "started_and_slot_busy": (409, STATE_TEXT),
    "slot_busy": (409, "На это занятие уже назначен экзамен «Соседний экзамен». Сначала перенесите или завершите его."),
    "group_changed_before_the_lock": (409, GROUP_CHANGED_TEXT),
    "started_and_slot_busy_only_under_the_lock": (409, "На это занятие уже назначен экзамен «Соседний экзамен». Сначала перенесите или завершите его."),
    "free": (200, "ok"),
}


@pytest.mark.parametrize("name", list(COMPARE))
@pytest.mark.parametrize("route", ROUTES)
def test_the_three_routes_refuse_one_input_in_one_way(client, db, monkeypatch, route, name):
    got = _outcome(SCENARIOS[name](client, db, route, monkeypatch))
    probe = os.environ.get("WE_D_PROBE")
    if probe:
        with open(probe, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"scenario": name, "route": route, "status": got[0], "detail": got[1]}, ensure_ascii=False) + "\n")
        return
    assert got == COMPARE[name]


def test_a_refused_binding_changes_nothing(client, db):
    exam = _exam(db, status="active")
    for route in ROUTES:
        assert _call(client, route, exam, day=_day()).status_code == 409
    db.expire_all()
    row = db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).first()
    assert row.status == "active" and row.access_code is None and row.scheduled_lesson_id is None


# ``assign`` without a lesson (publish=false: only the group of a draft) goes through the same gate, minus the lesson and the slot
def _assign_draft(client, exam):
    return client.post(f"{API}/exams/{exam.id}/assign", json={"group_id": TEST_GROUP_ID, "publish": False})


def test_assign_without_a_lesson_refuses_in_the_same_words(client, db):
    assert _outcome(_assign_draft(client, _exam(db, delivery="print"))) == (409, PRINT_TEXT)
    db.rollback()
    assert _outcome(_assign_draft(client, _exam(db, status="active", exam_id="EXAM_G2"))) == (409, STATE_TEXT)
    held = _exam(db, status="variants_ready", exam_id="EXAM_G3")
    _session(db, held)
    assert _outcome(_assign_draft(client, held)) == (409, ATTEMPTS_TEXT)


def test_assign_without_a_lesson_checks_the_group_under_the_lock_too(client, db, monkeypatch):
    exam = _exam(db)
    real = router.require_group_access

    async def moved_meanwhile(actor, group_id):
        db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).update({"group_id": OTHER_GROUP})
        db.commit()
        return await real(actor, group_id)

    monkeypatch.setattr(router, "require_group_access", moved_meanwhile)
    assert _outcome(_assign_draft(client, exam)) == (409, GROUP_CHANGED_TEXT)
    db.expire_all()
    assert db.query(GeneratedExam).filter(GeneratedExam.id == exam.id).first().group_id == OTHER_GROUP     # the twin's choice is not overwritten


def test_assign_of_a_lobby_without_a_lesson_still_asks_for_the_new_lesson(client, db):
    exam = _exam(db, status="lobby", lesson=(TEST_LESSON_ID, _day()))
    exam.access_code = "KEEP0001"
    db.commit()
    assert _outcome(_assign_draft(client, exam)) == (409, "Для экзамена в лобби выберите новое занятие и подтвердите перенос")
