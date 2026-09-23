"""The answer choices of one exam task, shared by online and printed delivery.

Stored options often write the key in another notation than
``correct_answer`` (``$2680$`` for ``2680``, ``\\dfrac{11}{50}`` for ``0.22``).
Appending the key whenever its exact string was missing showed students two
versions of one answer, and only one of them counted.  Here the option equal
to the key is served once, with the key as its value (the value is what gets
graded) and the author's notation as its display, so the key does not stand
out among options written in one style.

Mirrors diagnostic-service ``task_display.build_diag_answer_options``.
"""
from __future__ import annotations

import random
from typing import Any, Callable, Optional

from src.domain.answer_value import answer_value_key, answers_equivalent

Choice = tuple[str, str]  # (value graded on submit, display LaTeX or "")

_LETTER_OPTION_KEYS = frozenset({"a", "b", "c", "d", "а", "б", "в", "г"})


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _option_value(option: Any) -> str:
    if isinstance(option, dict):
        for key in ("text", "value", "content"):
            if option.get(key) is not None:
                return _text(option.get(key))
        return ""
    return _text(option)


def _option_latex(option: Any) -> str:
    if not isinstance(option, dict):
        return ""
    for key in ("latex", "value_latex", "content_latex", "text_latex"):
        if option.get(key):
            return _text(option.get(key))
    return ""


def _distractors(task: dict[str, Any]) -> list[Choice]:
    out: list[Choice] = []
    for item in task.get("distractor_meta") or []:
        if not isinstance(item, dict):
            continue
        # `text` and `content` are legacy shapes of the same field.
        value = _text(item.get("value") or item.get("text") or item.get("content"))
        latex = _text(item.get("value_latex") or item.get("text_latex") or item.get("content_latex"))
        out.append((value, latex))
    return out


def _latex_for_value(task: dict[str, Any], value: str) -> str:
    key = answer_value_key(value)
    if key and key == answer_value_key(task.get("correct_answer")):
        return _text(task.get("correct_answer_latex"))
    for distractor_value, latex in _distractors(task):
        if key and key == answer_value_key(distractor_value):
            return latex
    return ""


def _same_on_screen(first: Choice, second: Choice) -> bool:
    """Two options a student cannot tell apart: same value or same display."""
    return answers_equivalent(first[0], second[0]) or answers_equivalent(
        first[1] or first[0], second[1] or second[0]
    )


def _stored(task: dict[str, Any]) -> list[Choice]:
    raw = task.get("answer_options")
    if not isinstance(raw, list):
        return []
    raw_latex = task.get("answer_options_latex")
    out: list[Choice] = []
    for index, option in enumerate(raw):
        value = _option_value(option)
        latex = _option_latex(option)
        if not latex and isinstance(raw_latex, list) and index < len(raw_latex):
            latex = _text(raw_latex[index])
        out.append((value, latex or _latex_for_value(task, value)))
    return out


def choice_pairs(
    task: dict[str, Any],
    *,
    graded_as_key: Optional[Callable[[str], bool]] = None,
    max_distractors: Optional[int] = None,
) -> list[Choice]:
    """Return the task's choices in canonical (unshuffled) order.

    Exactly one returned value equals ``correct_answer``; no two choices look
    the same; empty choices are dropped.  ``graded_as_key`` is the delivery's
    grader: an option it would accept as the key is never offered as a second
    choice.  ``max_distractors`` caps choices built from ``distractor_meta``.
    An empty list means the task has fewer than two real choices and must be
    answered as open input.
    """
    correct = _text(task.get("correct_answer"))
    if not answer_value_key(correct):
        return []
    key: Choice = (correct, _latex_for_value(task, correct))

    def is_key_copy(choice: Choice) -> bool:
        return _same_on_screen(choice, key) or bool(
            graded_as_key and choice[0] != correct and graded_as_key(choice[0])
        )

    def is_new(choice: Choice, pairs: list[Choice]) -> bool:
        return (
            bool(answer_value_key(choice[0]))
            and not is_key_copy(choice)
            and not any(_same_on_screen(choice, served) for served in pairs)
        )

    stored = _stored(task)
    if len(stored) >= 2:
        # An author-supplied list is the exact UI source.  Its option equal to
        # the key is served as the key but keeps its own display: showing the
        # key's notation (0,22 among fractions) would give it away.
        key_index = next((i for i, choice in enumerate(stored) if _same_on_screen(choice, key)), None)
        pairs: list[Choice] = []
        for index, choice in enumerate(stored):
            if index == key_index:
                pairs.append((correct, choice[1] or choice[0]))
            elif is_new(choice, pairs):
                pairs.append(choice)
        if key_index is None:
            pairs.append(key)
    else:
        pairs = [key]
        for choice in _distractors(task):
            if max_distractors is not None and len(pairs) > max_distractors:
                break
            if is_new(choice, pairs):
                pairs.append(choice)
    return pairs if len(pairs) >= 2 else []


def is_letter_only(values: list[str]) -> bool:
    """Options that are bare letters point at choices printed in the question.

    Buttons are labelled A, B, C, D by position, so reordering such options
    would put the letter "C" under the button labelled "A".
    """
    return bool(values) and all(
        answer_value_key(value).strip(".)") in _LETTER_OPTION_KEYS for value in values
    )


def ordered(pairs: list[Choice], seed: Optional[str]) -> list[Choice]:
    """Shuffle deterministically by ``seed`` (``None`` keeps the canonical order)."""
    pairs = list(pairs)
    if seed is not None and not is_letter_only([value for value, _ in pairs]):
        random.Random(seed).shuffle(pairs)
    return pairs
