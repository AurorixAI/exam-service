"""Strict "same answer, different notation" comparison for choice options.

Mirrors diagnostic-service ``src/domain/answer_value.py`` (the services share
no package): both must agree on which stored options are one answer written
twice, or a task would be served differently in a diagnostic and an exam.

It decides which stored options a student could not tell apart, and is the
base of ``answer_utils.check_open_answer`` (typed answers, see there for the
rules added on top).  It is deliberately strict: when in doubt two answers
count as different.  Keep it a mirror of the diagnostic file.
"""
from __future__ import annotations

import re
from fractions import Fraction
from typing import Optional


def answer_value_key(value: object) -> str:
    text = str(value or "").strip()
    if len(text) >= 4 and text.startswith("$$") and text.endswith("$$"):
        text = text[2:-2].strip()
    elif len(text) >= 2 and text.startswith("$") and text.endswith("$"):
        text = text[1:-1].strip()
    text = text.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    text = text.replace("\\cdot", "*").replace("\\times", "*")
    text = text.replace("\\cup", "∪").replace("\\cap", "∩")
    text = text.replace("\\infty", "∞").replace("\\pi", "π")
    text = text.replace("\\le ", "\\leq ").replace("\\ge ", "\\geq ")
    text = text.replace("\\,", "").replace("\\;", "").replace("\\quad", "").replace("\\qquad", "")
    text = text.replace("{,}", ",")
    text = text.replace("\\left(", "(").replace("\\right)", ")")
    text = text.replace("\\left[", "[").replace("\\right]", "]")
    text = text.replace("\\left\\{", "{").replace("\\right\\}", "}")
    text = text.replace("\\sin", "sin").replace("\\cos", "cos").replace("\\tan", "tan").replace("\\tg", "tg")
    text = text.replace("\\log", "log").replace("\\ln", "ln")
    text = re.sub(r"\s+", "", text)
    return text.casefold()


# int() / Fraction() of a number with more than 4300 digits is a ValueError since Python 3.11; no school answer is
# this long, so a longer text is "not a plain number" (None) instead of an exception.
_MAX_NUMBER_CHARS = 1000
_LEFT_RIGHT_RE = re.compile(r"\\(?:left|right)(?![A-Za-z])")
_DIGIT_GAP_RE = re.compile(r"(?<=\d)\s+(?=\d)")
_DECIMAL_COMMA_RE = re.compile(r"(?<=\d)(?:\{,\}|,)(?=\d)")
# `\sin{\alpha}` renders exactly like `\sin \alpha`: braces around a single
# argument of a function (after an optional index or power) are invisible.
_FUNC_ARG_BRACES_RE = re.compile(
    r"(\\(?:arcsin|arccos|arctan|arctg|arcctg|sin|cos|tan|tg|cot|ctg|sec|csc|ln|lg|log|exp)"
    r"(?:[_^](?:\{[^{}]*\}|[0-9A-Za-z]))*)\s*\{(\\?[A-Za-z]+|\d+)\}"
)
_THIN_SPACE_RE = re.compile(r"\\[,;!]|\\q?quad(?![A-Za-z])")
# `\s*(\d+)?\s*` would let two adjacent `\s*` split a run of spaces n ways (quadratic on text a student typed);
# `\s*(?:(\d+)\s*)?` accepts the same strings in linear time.
_MIXED_LATEX_RE = re.compile(r"(-?)\s*(?:(\d+)\s*)?\\frac\{\s*(-?\d+)\s*\}\{\s*(\d+)\s*\}")
_MIXED_PLAIN_RE = re.compile(r"(-?)(\d+)\s+(\d+)\s*/\s*(\d+)")
_FRACTION_RE = re.compile(r"(-?\d+)\s*/\s*(\d+)")
_THOUSANDS_RE = re.compile(r"-?\d{1,3}(?: \d{3})+")
_DECIMAL_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _strip_trailing_punct(text: str) -> str:
    """Drop trailing whitespace, `.` and `;` (what `[\\s.;]+$` matched, without its quadratic backtracking)."""
    end = len(text)
    while end and (text[end - 1].isspace() or text[end - 1] in ".;"):
        end -= 1
    return text[:end]


def _notation_key(value: object) -> str:
    """answer_value_key after removing every math delimiter, not only outer ones.

    `$[-4$; $2]$` and `$[-4; 2]$` are the same value split differently into
    formulas, and `0{,}78` and `0.78` are one decimal written with a Russian
    and an English separator. Invisible braces (`\\sin{\\alpha}`) and a
    closing period or semicolon do not change an answer either. A space
    between two digits is kept as a marker so the mixed number `3 4/9` never
    collapses into the different fraction `34/9`.
    """
    text = str(value or "").replace("$", "").replace("−", "-")
    text = _LEFT_RIGHT_RE.sub("", text)
    text = _FUNC_ARG_BRACES_RE.sub(r"\1 \2", text)
    text = _DECIMAL_COMMA_RE.sub(".", text)
    text = _strip_trailing_punct(text)
    text = _DIGIT_GAP_RE.sub("␣", text)
    return answer_value_key(text)


def exact_number(value: object) -> Optional[Fraction]:
    """Exact rational value of a plain number, fraction or mixed number.

    Recognises only unambiguous numeric forms; anything else (expressions,
    intervals, lists, percentages) returns None so it can never be matched
    by value. There is deliberately no floating-point tolerance: 1/3 and
    0,33 are different answers.
    """
    text = str(value or "").replace("$", "").replace("{,}", ",").replace("−", "-")
    text = text.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    text = _THIN_SPACE_RE.sub(" ", text).strip()
    if len(text) > _MAX_NUMBER_CHARS:
        return None
    m = _MIXED_LATEX_RE.fullmatch(text)
    if m:
        sign, whole, num, den = m.groups()
        if int(den) == 0 or (whole and num.startswith("-")):
            return None
        result = Fraction(int(num), int(den)) + (int(whole) if whole else 0)
        return -result if sign else result
    m = _MIXED_PLAIN_RE.fullmatch(text)
    if m:
        sign, whole, num, den = m.groups()
        if int(den) == 0:
            return None
        result = int(whole) + Fraction(int(num), int(den))
        return -result if sign else result
    m = _FRACTION_RE.fullmatch(text)
    if m:
        return Fraction(int(m.group(1)), int(m.group(2))) if int(m.group(2)) else None
    if _THOUSANDS_RE.fullmatch(text):
        return Fraction(text.replace(" ", ""))
    if _DECIMAL_RE.fullmatch(text):
        return Fraction(text.replace(",", "."))
    return None


def answers_equivalent(first: object, second: object) -> bool:
    """True only when two stored answers are provably the same value.

    Used to recognise one answer written in two notations (`0,22` and
    `\\dfrac{11}{50}`, `$[-4$; $2]$` and `$[-4; 2]$`) among a task's options.
    It is intentionally strict: a false positive would let a wrong option be
    shown as correct, so anything not provably equal counts as different.
    """
    a, b = _notation_key(first), _notation_key(second)
    if not a or not b:
        return False
    if a == b:
        return True
    na, nb = exact_number(first), exact_number(second)
    return na is not None and nb is not None and na == nb
