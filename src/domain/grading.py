"""Which grader applies to a task: a chosen option or a typed answer.

A student picks an option when the task is served with choices, and types the
answer otherwise.  That is decided per task, exactly as the router decides it
when it builds the student's task list:

* ``mc_mode == "all_open"`` - every task is typed;
* otherwise a task is a choice task if it has stored options or distractors
  AND ``choice_pairs`` finds at least two real choices; the rest are typed.

A chosen option is graded by ``check_answer`` (unchanged).  A typed answer is
graded by ``check_open_answer``, which understands the LaTeX the bank stores
its keys in.

One verdict everywhere.  Grading happens once, at submit, and the verdict of
every task is stored next to the session's distractor hits under a reserved
key (``GRADING_KEY``) together with the grader version - no migration, the
column is a JSON dict and every reader skips entries without ``hit``.  Results,
analytics and the report show the stored verdicts.  A session graded before
this existed (no marker, or another version) is regraded once by
``regrade_if_stale``, which also rewrites its ``score`` / ``correct_count``
from the same verdicts, so a shown verdict and a shown score never come from
different graders.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from src.domain.answer_utils import check_answer, check_open_answer
from src.domain.choice_options import Choice, choice_pairs

log = logging.getLogger(__name__)

# 1 = the old grader for every task (sessions that carry no marker); 2 = check_task_answer.
GRADER_VERSION = 2
GRADING_KEY = "_grading"
POINTS_PER_TASK = 10

# Distractors offered next to the key in an online exam (key + 3).
MAX_DISTRACTORS = 3


def should_show_mc(task: dict, mc_mode: Optional[str] = "auto") -> bool:
    if mc_mode == "all_open":
        return False
    return bool(task.get("answer_options") or task.get("distractor_meta"))


def mc_choices(task: dict) -> list[Choice]:
    """(value, display) choices; the key once, graded by this service's grader."""
    correct = str(task.get("correct_answer") or "")
    return choice_pairs(
        task,
        graded_as_key=lambda value: check_answer(value, correct),
        max_distractors=MAX_DISTRACTORS,
    )


def is_open_input(task: dict, mc_mode: Optional[str] = "auto") -> bool:
    """True when the student types the answer to this task."""
    return not (should_show_mc(task, mc_mode) and bool(mc_choices(task)))


def check_task_answer(task: dict, student: str, mc_mode: Optional[str] = "auto") -> bool:
    """Grade one answer with the grader that matches how the task was served.

    Never raises: one answer the grader cannot handle is wrong, it must not stop the grading of a whole exam
    (submit, the timer's auto-close and the teacher's close all grade every session in one transaction).  The
    log line names the task and the error type only - never the student's text.
    """
    try:
        correct = str(task.get("correct_answer") or "")
        if is_open_input(task, mc_mode):
            return check_open_answer(student, correct)
        return check_answer(student, correct)
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.warning("grading failed for task %s: %s", task.get("task_id"), type(exc).__name__)
        return False


def session_tasks(exam: Any, session: Any) -> list[dict]:
    """The tasks of the variant this session was given."""
    variants = (exam.variants or []) if exam else []
    variant = next(
        (v for v in variants if v.get("variant_number") == session.variant_number),
        variants[0] if variants else None,
    )
    return (variant.get("tasks", []) if variant else []) or []


def grade_session_tasks(tasks: list[dict], answers: dict, mc_mode: Optional[str] = "auto") -> dict[str, bool]:
    """{task_id: verdict} for every task of the variant (an unanswered task is wrong)."""
    answers = answers if isinstance(answers, dict) else {}
    verdicts: dict[str, bool] = {}
    for task in tasks:
        task_id = task.get("task_id", "")
        student = answers.get(task_id, "")
        verdicts[task_id] = check_task_answer(task, student if isinstance(student, str) else "", mc_mode)
    return verdicts


def with_grading(hits: Optional[dict], verdicts: dict[str, bool]) -> dict:
    """Distractor hits plus the grading marker, as stored in ``session.distractor_analytics``."""
    stored = {key: value for key, value in (hits or {}).items() if key != GRADING_KEY}
    stored[GRADING_KEY] = {"v": GRADER_VERSION, "verdicts": dict(verdicts)}
    return stored


def stored_verdicts(distractor_analytics: Any) -> Optional[dict[str, bool]]:
    """The verdicts saved at grading time, or None for a session graded by another grader version."""
    marker = distractor_analytics.get(GRADING_KEY) if isinstance(distractor_analytics, dict) else None
    if isinstance(marker, dict) and marker.get("v") == GRADER_VERSION and isinstance(marker.get("verdicts"), dict):
        return marker["verdicts"]
    return None


def regrade_if_stale(session: Any, exam: Any) -> bool:
    """Regrade a submitted session that has no verdicts of the current grader; True if it was changed.

    Rewrites ``score`` and ``correct_count`` from the same verdicts it stores, drops stored distractor hits of tasks
    that are now right, and does not commit (the caller owns the transaction).
    """
    if session.status != "submitted" or exam is None or stored_verdicts(session.distractor_analytics) is not None:
        return False
    mc_mode = (exam.settings or {}).get("mc_mode", "auto")
    verdicts = grade_session_tasks(session_tasks(exam, session), session.answers or {}, mc_mode)
    # Old rows may hold anything in this column (NULL, a list, a string): that is "stale, no hits", not an error.
    old_hits = session.distractor_analytics if isinstance(session.distractor_analytics, dict) else {}
    hits = {
        task_id: hit
        for task_id, hit in old_hits.items()
        if task_id != GRADING_KEY and not verdicts.get(task_id)
    }
    correct = sum(verdicts.values())
    session.correct_count = correct
    session.score = correct * POINTS_PER_TASK
    session.distractor_analytics = with_grading(hits, verdicts)
    return True
