"""
Shared answer comparison utilities used by both router and report_generator.
Kept in the domain layer to avoid circular imports (domain ← api is forbidden).

Two graders live here and must not be mixed up:

* ``check_answer`` - the original grader.  It is what a student's *chosen
  option* is compared with (the option text is the stored key or a stored
  distractor, so both sides share one notation), and what ``choice_pairs``
  uses to keep a key lookalike out of the options.  Unchanged.
* ``check_open_answer`` - for a *typed* answer.  The bank stores keys as LaTeX
  (``$\\dfrac{1}{2}$``, ``$0{,}5$``, ``$\\varnothing$``), a student types
  ``1/2``, ``0,5``, ``нет корней``.  See ``src/domain/grading.py`` for which
  tasks are graded this way.
"""
from __future__ import annotations

import re
from math import gcd

from src.domain.answer_value import _MAX_NUMBER_CHARS, _THIN_SPACE_RE, _notation_key, answers_equivalent


def normalise_answer(answer: str) -> str:
    return answer.strip().lower().replace(",", ".") if answer else ""


def check_answer(student: str, correct: str) -> bool:
    s, c = normalise_answer(student), normalise_answer(correct)
    if s == c:
        return True
    try:
        return abs(float(s) - float(c)) < 0.01
    except (ValueError, TypeError):
        return False


# --- typed (free-input) answers ---------------------------------------------------------------------------------
#
# Rules (1, 2 and 4 are applied to the key and to the typed answer alike):
#   1. `answers_equivalent` of the diagnostic service (mirrored in answer_value.py): every `$` is dropped,
#      `\dfrac` = `\frac`, a decimal comma / `{,}` = a point, spaces and letter case are ignored, a trailing
#      `.`/`;` is ignored, a fraction / decimal / mixed number is compared BY EXACT VALUE (1/2 = \dfrac{1}{2} = 0,5 =
#      0.50; 3 4/9 = 31/9; 3 4/9 != 34/9).  No numeric tolerance: 0.333 is not 0.33.
#   2. A braced group of ONE character after ^ or _ is invisible: x^{3} = x^3, a_{2} = a_2.  A longer group is
#      not touched, so x^{13} (exponent 13) differs from x^13 (x^1 followed by 3).
#   3. Equal VALUE is not enough for a fraction that is not in lowest terms: `42/10`, `4/10` or `3 7/4` are
#      not accepted for `\dfrac{21}{5}` / `\dfrac{2}{5}` / `4 \dfrac{3}{4}`, because "reduce the fraction" tasks keep exactly
#      such a value as their designed distractor (18 tasks of the bank collide without this rule).  A typed
#      fraction with the key's own numerator and denominator is accepted (`4/10` for a key `\dfrac{4}{10}`), so an
#      unreduced key still matches itself; a reduced typed answer is always fine.
#   4. The empty set has one spelling per meaning: ∅, \varnothing, \emptyset, "пустое множество", and the two
#      phrases a student writes for it, "нет корней" / "корней нет" and "нет решений" / "решений нет" (also
#      `x ∈ ∅`).  Only a WHOLE answer is read this way: "нет корней при a<0" is not the empty set.
#      Deliberately NOT merged: "нет действительных корней" (over the complex numbers there are roots), and
#      "0" / "0 корней" (a count, not a set).
#   5. A typed answer longer than MAX_OPEN_ANSWER_CHARS is wrong without being parsed (a number, a short
#      expression or a few words are shorter).  In the bank 96 raw keys are longer than that, 78 once the `$` are
#      removed (the length that is compared); they are texts of proofs, nobody types them.
# Everything else is compared as written: when in doubt two answers are different.
# Known limits (owner decisions pending): letter case is ignored (F = f, as in the diagnostic), and a task that asks
# for a particular form ("запишите в виде смешанного числа") accepts any equal value in lowest terms (7/2 for 3 1/2).
MAX_OPEN_ANSWER_CHARS = 300
_SINGLE_TOKEN_BRACES_RE = re.compile(r"([\^_])\{\s*([^\W_])\s*\}")
_FRACTION_FORM_RES = (
    # latex: [-][whole]\frac{num}{den}   (`\s*(?:(\d+)\s*)?` - one `\s*` per gap, so it is linear)
    re.compile(r"-?\s*(?:(\d+)\s*)?\\frac\{\s*(-?\d+)\s*\}\{\s*(\d+)\s*\}"),
    # plain mixed number: [-]whole num/den
    re.compile(r"-?(\d+)\s+(-?\d+)\s*/\s*(\d+)"),
    # plain fraction: num/den (no whole part)
    re.compile(r"()(-?\d+)\s*/\s*(\d+)"),
)
_EMPTY_SET_PREFIX_RE = re.compile(r"^[a-zа-я]\s*(?:\\in|∈)\s*")
_EMPTY_SET_FORMS = frozenset(
    {
        "∅",
        "\\varnothing",
        "\\emptyset",
        "пустое множество",
        "нет корней",
        "корней нет",
        "нет решений",
        "решений нет",
    }
)


def _is_empty_set(value: str) -> bool:
    text = re.sub(r"\s+", " ", value.replace("$", "").casefold()).strip()
    text = text.rstrip(".;").strip()
    text = _EMPTY_SET_PREFIX_RE.sub("", text).strip()
    return text in _EMPTY_SET_FORMS


def _fraction_shape(value: str):
    """(whole, numerator, denominator) exactly as written, or None if the text is not a fraction / mixed number."""
    # The same normalisation as `answer_value.exact_number`: whatever it reads as a fraction must be seen here too
    # (`4/10\,`, `4/10\quad` and `42/10\;` are the fraction 4/10, not something to be graded by value for free).
    text = value.replace("$", "").replace("{,}", ",").replace("−", "-")
    text = text.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    text = _THIN_SPACE_RE.sub(" ", text).strip()
    if len(text) > _MAX_NUMBER_CHARS:
        return None
    for pattern in _FRACTION_FORM_RES:
        match = pattern.fullmatch(text)
        if match:
            whole, num, den = match.groups()
            return (int(whole) if whole else 0, abs(int(num)), int(den))
    return None


def _in_lowest_terms(shape) -> bool:
    whole, num, den = shape
    return den > 1 and gcd(num, den) == 1 and (not whole or num < den)


def _value_match_allowed(typed: str, key: str) -> bool:
    """A match that is only by value needs a typed fraction in lowest terms, or the key's own numerator / denominator."""
    shape = _fraction_shape(typed)
    return shape is None or _in_lowest_terms(shape) or shape == _fraction_shape(key)


def check_open_answer(student: str, correct: str) -> bool:
    """True only when a typed answer provably says the same as the stored key.

    Never raises: an answer longer than ``MAX_OPEN_ANSWER_CHARS`` is wrong without being parsed, and any parser
    failure (``int`` of a number with > 4300 digits is a ValueError since Python 3.11) means "not proven" = wrong.
    """
    typed, key = str(student or ""), str(correct or "")
    if not typed.strip() or not key.strip() or len(typed) > MAX_OPEN_ANSWER_CHARS:
        return False
    try:
        return _open_answer_matches(typed, key)
    except (ValueError, OverflowError, RecursionError):
        return False


def _open_answer_matches(typed: str, key: str) -> bool:
    if _is_empty_set(typed) and _is_empty_set(key):
        return True
    typed = _SINGLE_TOKEN_BRACES_RE.sub(r"\1\2", typed)
    key = _SINGLE_TOKEN_BRACES_RE.sub(r"\1\2", key)
    if not answers_equivalent(typed, key):
        return False
    # Same text as the key (rule 1) is a match; equal only by value needs a fraction in lowest terms (rule 3).
    return _notation_key(typed) == _notation_key(key) or _value_match_allowed(typed, key)
