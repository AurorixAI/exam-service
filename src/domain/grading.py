"""Which grader applies to a task: a chosen option or a typed answer.

A student picks an option when the task is served with choices, and types the
answer otherwise.  That is decided per task, exactly as the router decides it
when it builds the student's task list:

* ``mc_mode == "all_open"`` - every task is typed;
* otherwise a task is a choice task if it has stored options or distractors
  AND ``choice_pairs`` finds at least two real choices; the rest are typed.

A chosen option is graded by ``check_answer`` (unchanged).  A typed answer is
graded by ``check_open_answer``, which understands the LaTeX the bank stores
its keys in.  Submit, results, analytics and the report must call
``check_task_answer`` so a student sees one verdict everywhere.
"""
from __future__ import annotations

from typing import Optional

from src.domain.answer_utils import check_answer, check_open_answer
from src.domain.choice_options import Choice, choice_pairs

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
    """Grade one answer with the grader that matches how the task was served."""
    correct = str(task.get("correct_answer") or "")
    if is_open_input(task, mc_mode):
        return check_open_answer(student, correct)
    return check_answer(student, correct)
