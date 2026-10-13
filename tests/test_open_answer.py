"""Typed (free-input) answers against the LaTeX keys the bank stores.

The bank keeps keys as ``$\\dfrac{1}{2}$``, ``$0{,}5$``, ``$\\varnothing$`` (32 744 of 34 606 active keys contain a
``$``).  A student who types ``1/2``, ``0,5`` or ``нет корней`` must be graded as right; a student who picks an
option from a list is graded exactly as before.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("APP_ENV", "testing")

import pytest

from src.api.router import _build_mc_options
from src.domain.answer_utils import check_answer, check_open_answer
from src.domain.answer_value import answers_equivalent
from src.domain.grading import check_task_answer, is_open_input
from src.domain.report_generator import _aggregate
from tests.conftest import make_exam_in_db, make_session_in_db, make_task
from tests.open_answer_bank import BANK_DEFECTS, regression

FIXTURE = Path(__file__).parent / "fixtures" / "open_answer_bank_sample.json"


# ── accepted: the same answer in another notation ─────────────────────────────────────────────────────────────
ACCEPTED = [
    # `$` wrapper
    ("2", "$2$"),
    ("-5", "$-5$"),
    (" 42 ", "$42$"),
    ("−5", "$-5$"),                                   # typographic minus
    ("5.", "$5$"),                                    # closing period
    ("12 км/ч", "$12$ км/ч"),                         # `$` in the middle of the key
    ("2 дм 3 см", "$2$ дм $3$ см"),
    ("x=3", "$x = 3$"),
    ("x>6", "$x > 6$"),
    ("(1; 2)", "$(1; 2)$"),
    ("[-4;2]", "$[-4$; $2]$"),
    ("ДА", "да"),
    # decimal separator
    ("0,5", "$0{,}5$"),
    ("0.5", "$0{,}5$"),
    ("0,5", "0.5"),
    ("2,50", "$2{,}5$"),
    ("1 000", "$1000$"),                              # thousands space
    # fractions and mixed numbers by value
    ("1/2", "$\\dfrac{1}{2}$"),
    ("1/2", "$\\frac{1}{2}$"),
    ("\\dfrac{1}{2}", "1/2"),
    ("0,5", "$\\dfrac{1}{2}$"),
    ("0,22", "$\\dfrac{11}{50}$"),
    ("-1/2", "$-\\dfrac{1}{2}$"),
    ("-1/2", "$\\dfrac{-1}{2}$"),
    ("5 8/9", "$5 \\dfrac{8}{9}$"),
    ("5 8/9", "$5\\dfrac{8}{9}$"),
    ("53/9", "$5\\dfrac{8}{9}$"),                     # improper = mixed, by value
    ("3,5", "$3\\dfrac{1}{2}$"),
    ("2/5", "$\\dfrac{4}{10}$"),                      # reduced typed answer for an unreduced key
    ("4/10", "$\\dfrac{4}{10}$"),                     # a key that is itself unreduced still matches itself
    # braces around one character are invisible
    ("x^2", "$x^{2}$"),
    ("x^{2}", "$x^2$"),
    ("a_1+a_2", "$a_{1} + a_{2}$"),
    ("x^{13}", "$x^{13}$"),
    # the empty set
    ("нет корней", "$\\varnothing$"),
    ("Нет решений.", "$\\varnothing$"),
    ("∅", "$\\varnothing$"),
    ("пустое множество", "$\\emptyset$"),
    ("решений нет", "нет корней"),
    ("∅", "$x \\in \\varnothing$"),
    ("нет корней", "$x \\in \\varnothing$"),
]

# ── rejected: a different answer, or one that cannot be proven the same ───────────────────────────────────────
REJECTED = [
    # other numbers, sign
    ("3", "$2$"),
    ("-2", "$2$"),
    ("2", "$-2$"),
    ("2/1", "$\\dfrac{1}{2}$"),
    ("1/2", "$2$"),
    ("-1/2", "$\\dfrac{1}{2}$"),
    ("1/3", "$0{,}33$"),
    ("0,33", "$\\dfrac{1}{3}$"),
    ("0.333", "0.33"),                                # no numeric tolerance (the choice grader has 0.01)
    ("0.2", "0.21"),
    ("25%", "$25$"),
    ("5/0", "$5$"),
    # exponents and indices
    ("x^13", "$x^{13}$"),
    ("x^{13}", "$x^{1}$"),
    ("x^2", "$x^{3}$"),
    ("2x", "$x^{2}$"),
    # intervals, signs, lists
    ("(1; 2)", "$[1; 2]$"),
    ("[1; 2)", "$[1; 2]$"),
    ("(1; 2]", "$[1; 2]$"),
    ("x>6", "$x < 6$"),
    ("x>6", "$x \\geq 6$"),
    ("1,2", "$1; 2$"),
    ("12", "$1; 2$"),
    # a fraction not in lowest terms is not the answer to "reduce"
    ("42/10", "$\\dfrac{21}{5}$"),
    ("4/10", "$\\dfrac{2}{5}$"),
    ("3 7/4", "$4 \\dfrac{3}{4}$"),
    ("34/9", "$3 \\dfrac{4}{9}$"),
    ("5/1", "$5$"),
    # the empty set is not a number, and a condition is not the empty set
    ("0", "$\\varnothing$"),
    ("1", "$\\varnothing$"),
    ("нет действительных корней", "$\\varnothing$"),
    ("нет корней при a<0", "$\\varnothing$"),
    ("∅", "$\\mathbb{R}$"),
    # words and blanks
    ("да", "нет"),
    ("a", "b"),
    ("", "$2$"),
    ("   ", "$2$"),
    ("2", ""),
]


@pytest.mark.parametrize("typed, key", ACCEPTED)
def test_typed_answer_is_accepted(typed, key):
    assert check_open_answer(typed, key) is True


@pytest.mark.parametrize("typed, key", REJECTED)
def test_typed_answer_is_rejected(typed, key):
    assert check_open_answer(typed, key) is False


def test_there_are_enough_cases():
    assert len(ACCEPTED) + len(REJECTED) >= 40
    assert len(REJECTED) >= 20


def test_lowest_terms_rule_is_about_the_typed_answer_only():
    """`42/10` is no answer to a key `21/5`, but a reduced `21/5` is an answer to a key stored as `42/10`."""
    assert check_open_answer("42/10", "$\\dfrac{21}{5}$") is False
    assert check_open_answer("21/5", "$\\dfrac{42}{10}$") is True
    assert check_open_answer("42/10", "$\\dfrac{42}{10}$") is True
    assert check_open_answer("84/20", "$\\dfrac{42}{10}$") is False


def test_non_string_input_never_raises():
    assert check_open_answer(None, "$2$") is False
    assert check_open_answer(2, "$2$") is True
    assert check_open_answer("2", None) is False


# ── which grader applies ──────────────────────────────────────────────────────────────────────────────────────
def _task(answer="$\\dfrac{1}{2}$", distractors=("$\\dfrac{1}{3}$", "$2$", "$\\dfrac{2}{3}$")):
    task = make_task("T1", answer=answer)
    task["distractor_meta"] = [{"value": value, "error_type": "x", "explanation": "e"} for value in distractors]
    return task


def test_all_open_mode_is_typed_input():
    assert is_open_input(_task(), "all_open") is True
    assert is_open_input(_task(), "auto") is False


def test_auto_mode_task_without_choices_is_typed_input():
    assert is_open_input(make_task("T2", answer="$2$"), "auto") is True
    only_a_copy_of_the_key = _task("$2$", distractors=("2",))
    assert is_open_input(only_a_copy_of_the_key, "auto") is True


def test_typed_answer_in_all_open_mode_uses_the_latex_aware_grader():
    task = _task()
    assert check_task_answer(task, "1/2", "all_open") is True
    assert check_task_answer(task, "0,5", "all_open") is True
    assert check_task_answer(task, "1/3", "all_open") is False
    assert check_task_answer(task, "", "all_open") is False


def test_choice_mode_grading_is_exactly_the_old_grader():
    """In choice mode nothing changed: the option text is compared as before."""
    task = _task()
    options = _build_mc_options(task, shuffle=False)
    assert "$\\dfrac{1}{2}$" in options
    for option in options:
        assert check_task_answer(task, option, "auto") == check_answer(option, task["correct_answer"])
    assert check_task_answer(task, "$\\dfrac{1}{2}$", "auto") is True
    assert check_task_answer(task, "$\\dfrac{1}{3}$", "auto") is False
    # a typed look-alike is NOT accepted by a choice task (it was not before either)
    assert check_task_answer(task, "1/2", "auto") is False
    assert check_task_answer(task, "0,5", "auto") is False
    # the old 0.01 tolerance of the choice grader is kept for choice tasks only
    decimal = _task("0.33", distractors=("0.5", "0.25", "0.1"))
    assert check_task_answer(decimal, "0.333", "auto") is True
    assert check_task_answer(decimal, "0.333", "all_open") is False


# ── the verdict is the same on submit, in the results page and in the report ──────────────────────────────────
def _exam_with(db, mc_mode, task):
    exam = make_exam_in_db(db, tasks=[task])
    exam.settings = {"time_minutes": 45, "mc_mode": mc_mode}
    db.commit()
    return exam


@pytest.mark.parametrize("mc_mode, typed, expected", [("all_open", "1/2", 1), ("all_open", "1/3", 0), ("auto", "1/2", 0)])
def test_submit_results_and_report_agree(client, db, mc_mode, typed, expected):
    task = _task()
    exam = _exam_with(db, mc_mode, task)
    session = make_session_in_db(db, exam)
    submitted = client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={"answers": {"T1": typed}})
    assert submitted.status_code == 200
    assert submitted.json()["correct_count"] == expected
    assert submitted.json()["score"] == 10 * expected

    assert client.post(f"/api/v1/exams/{exam.id}/close").status_code == 200
    results = client.get(f"/api/v1/exam-sessions/{session.id}/results").json()
    assert results["tasks"][0]["is_correct"] is bool(expected)

    db.refresh(session)
    report = _aggregate(exam, [session])
    assert report["task_analytics"][0]["correct_count"] == expected
    analytics = client.get(f"/api/v1/exams/{exam.id}/analytics").json()
    assert analytics["task_analytics"][0]["correct_count"] == expected


def test_open_task_in_auto_mode_is_graded_with_the_latex_aware_grader(client, db):
    exam = _exam_with(db, "auto", make_task("T1", answer="$\\varnothing$"))
    session = make_session_in_db(db, exam)
    data = client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={"answers": {"T1": "нет корней"}}).json()
    assert data["correct_count"] == 1


# ── regression on real bank tasks ─────────────────────────────────────────────────────────────────────────────
def _sample():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_sample_keys_typed_without_dollar_are_accepted_and_distractors_are_not():
    stats = regression(_sample())
    assert stats["tasks"] >= 60
    assert stats["self_failures"] == []
    # before: the old grader accepted almost none of these keys typed without `$`
    assert stats["self_typed_ok_legacy"] < stats["tasks"] // 2
    assert stats["self_typed_ok_open"] == stats["tasks"]
    assert {violation[0] for violation in stats["violations"]} <= set(BANK_DEFECTS)


def test_value_equal_distractors_of_the_sample_are_not_accepted():
    """18 bank tasks keep an unreduced copy of the key (`42/10` for `21/5`) as a designed distractor."""
    colliding = [
        row for row in _sample()
        if row["id"] not in BANK_DEFECTS and any(answers_equivalent(d, row["correct_answer"]) for d in row["distractors"])
    ]
    assert len(colliding) >= 15, "fixture lost its unreduced-fraction tasks"
    for row in colliding:
        for distractor in row["distractors"]:
            assert check_open_answer(distractor, row["correct_answer"]) is False, row["id"]


@pytest.mark.skipif(not os.environ.get("ALGO_BANK_JSONL"), reason="full-bank dump not provided (ALGO_BANK_JSONL)")
def test_full_bank_regression():
    rows = [json.loads(line) for line in open(os.environ["ALGO_BANK_JSONL"], encoding="utf-8") if line.strip()]
    stats = regression(rows)
    assert stats["self_failures"] == []
    assert {violation[0] for violation in stats["violations"]} <= set(BANK_DEFECTS)
