"""How exam answer choices are served: one key, no look-alikes, stable order.

Every case below was found in the live task bank, where the previous builder
appended the key whenever its exact string was missing from the options.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("APP_ENV", "testing")

import pytest

from src.api.router import _build_mc_options, _build_mc_options_latex, _check_answer
from src.domain.answer_value import answers_equivalent, exact_number
from src.domain.print_package import build_default_print_layout, build_print_snapshot, print_choice_options
from src.domain.print_pdf import _teacher_choice_label


def _served(task, seed=None):
    options = _build_mc_options(task, shuffle=seed is not None, seed=seed)
    return options, _build_mc_options_latex(task, options)


def _graded(task, options):
    return [_check_answer(option, task["correct_answer"]) for option in options]


# ── one answer in two notations ─────────────────────────────────────────────

def test_key_stored_with_math_delimiters_is_served_once_and_graded_correct():
    # Before: "2680" was prepended next to "$2680$"; choosing "$2680$" was wrong.
    task = {"correct_answer": "2680", "correct_answer_latex": "$2680$",
            "answer_options": ["$2068$", "$2468$", "$20680$", "$2680$"]}

    options, latex = _served(task)

    assert len(options) == 4
    assert _graded(task, options) == [False, False, False, True]
    assert latex[3] == "$2680$"


def test_key_in_another_notation_keeps_the_authors_display():
    task = {"correct_answer": "0.22", "correct_answer_latex": "$0{,}22$",
            "answer_options": [r"$\dfrac{11}{50}$", r"$\dfrac{1}{4}$", r"$\dfrac{1}{5}$"]}

    options, latex = _served(task)

    assert options[0] == "0.22" and latex[0] == r"$\dfrac{11}{50}$"
    assert _graded(task, options) == [True, False, False]


def test_decimal_comma_and_dot_are_one_answer():
    key = "$0,78^{-0,7} > 0,78^{-0,6}$"
    task = {"correct_answer": key, "correct_answer_latex": key,
            "answer_options": ["$0.78^{-0.7} > 0.78^{-0.6}$", "$0.78^{-0.7} < 0.78^{-0.6}$",
                               "$0.78^{-0.7} = 0.78^{-0.6}$", "$0.78^{-0.7} > 0.78^{-0.6}$"]}

    options, _ = _served(task)

    assert len(options) == 3
    assert _graded(task, options).count(True) == 1


def test_key_repeated_without_its_closing_period_is_one_answer():
    key = "Если $a < -4$, то $x \\in (4; -a]$; если $a > -4$, то $x \\in [-a; 4)$."
    task = {"correct_answer": key, "correct_answer_latex": key,
            "distractor_meta": [{"value": "Если $a < -4$, то $x \\in [-a; 4)$; если $a > -4$, то $x \\in (4; -a]$"},
                                {"value": "Если $a < -4$, то $x \\in (4; -a]$; если $a > -4$, то $x \\in [-a; 4)$"},
                                {"value": "Если $a < -4$, то $x \\in \\varnothing$"}]}

    options, _ = _served(task)

    assert len(options) == 3
    assert _graded(task, options).count(True) == 1


def test_repeated_copies_of_the_key_are_served_once():
    task = {"correct_answer": "$40$", "correct_answer_latex": "$40$",
            "answer_options": ["$40$", "$18$", "$40$"],
            "distractor_meta": [{"value": "$40$"}]}

    options, _ = _served(task)

    assert options == ["$40$", "$18$"]


def test_distractor_displayed_like_the_key_is_not_offered():
    # Historical row: the distractor's value is "4/p" but its display is the key.
    key = r"$\dfrac{1}{4p}$"
    task = {"correct_answer": key, "correct_answer_latex": key,
            "distractor_meta": [{"value": "$4/p$", "value_latex": key},
                                {"value": r"$\frac{1}{4pq}$", "value_latex": r"$\dfrac{1}{4pq}$"},
                                {"value": r"$\frac{p}{4}$", "value_latex": r"$\dfrac{p}{4}$"}]}

    options, latex = _served(task)

    assert len(latex) == 3 and latex.count(key) == 1
    assert _graded(task, options).count(True) == 1


def test_option_the_grader_would_accept_is_not_a_second_choice():
    # check_answer accepts 0.333 for 0.33 (tolerance 0.01): it must not be
    # offered as a separate, "wrong" button.
    task = {"correct_answer": "0.33", "answer_options": ["0.333", "0.5", "0.25"]}

    options, _ = _served(task)

    assert "0.333" not in options
    assert _graded(task, options).count(True) == 1


def test_distinct_answers_are_never_merged():
    task = {"correct_answer": "1/3", "answer_options": ["$0{,}33$", "$1/3$", "$0{,}3$", "$3$"]}

    options, _ = _served(task)

    assert len(options) == 4
    assert _graded(task, options).count(True) == 1


# ── choices built from distractor_meta ──────────────────────────────────────

def test_legacy_text_distractors_never_become_empty_buttons():
    task = {"correct_answer": "$(-\\infty; 0)$", "correct_answer_latex": "$(-\\infty; 0)$",
            "answer_options": [],
            "distractor_meta": [{"text": "(0; \\infty)", "value_latex": "$(0; \\infty)$"},
                                {"text": "(-\\infty; +\\infty)"}, {"text": "(-1; 0)"}]}

    options, latex = _served(task)

    assert all(option.strip() for option in options)
    assert len(options) == 4
    assert latex[1] == "$(0; \\infty)$"


def test_built_choices_skip_duplicates_before_applying_the_cap():
    task = {"correct_answer": "1/2",
            "distractor_meta": [{"value": "0,5"}, {"value": "1/3"}, {"value": "2"}, {"value": "3"}, {"value": "4"}]}

    options, _ = _served(task)

    assert options == ["1/2", "1/3", "2", "3"]


def test_only_the_key_left_means_open_input():
    task = {"correct_answer": "1/2", "distractor_meta": [{"value": "0,5"}, {"value": r"$\dfrac{1}{2}$"}]}

    assert _build_mc_options(task, shuffle=True, seed="s") == []


def test_yes_no_with_one_distractor_is_two_buttons():
    task = {"correct_answer": "нет", "distractor_meta": [{"value": "да"}]}

    options, _ = _served(task, seed="s")

    assert sorted(options) == ["да", "нет"]


# ── stable per-attempt order ────────────────────────────────────────────────

STORED = {"task_id": "t1", "correct_answer": "4", "answer_options": ["4", "3", "5", "6"],
          "answer_options_latex": ["$4$", "$3$", "$5$", "$6$"]}
BUILT = {"task_id": "t1", "correct_answer": "4", "correct_answer_latex": "$4$",
         "distractor_meta": [{"value": "3", "value_latex": "$3$"}, {"value": "5", "value_latex": "$5$"},
                             {"value": "6", "value_latex": "$6$"}]}


@pytest.mark.parametrize("task", [STORED, BUILT], ids=["stored", "built"])
def test_key_position_is_uniform_and_stable(task):
    positions = Counter()
    for n in range(2000):
        seed = f"EXAM_1:SES_{n}:t1"
        options, latex = _served(task, seed)
        assert (options, latex) == _served(task, seed)
        assert latex == [f"${value}$" for value in options]
        assert _graded(task, options) == [option == "4" for option in options]
        positions[options.index("4")] += 1
    assert set(positions) == {0, 1, 2, 3}
    assert all(350 <= count <= 650 for count in positions.values()), positions


def test_letter_options_keep_their_printed_order():
    task = {"correct_answer": "$B$", "answer_options": ["$A$", "$B$", "$C$", "$D$"]}
    for n in range(20):
        assert _build_mc_options(task, shuffle=True, seed=f"SES_{n}") == ["$A$", "$B$", "$C$", "$D$"]


# ── printed exams ───────────────────────────────────────────────────────────

def _printed_task(**task):
    variants = [{"variant_number": 1, "tasks": [{"task_id": "p1", "skill_name": "Дроби",
                                                 "question_text": "Вычислите.", **task}]}]
    snapshot = build_print_snapshot(
        exam_id="exam-1", title="Контрольная", subject="Математика", group_name="6А",
        class_level=6, lesson_id="lesson-1", lesson_date="2026-09-23", total_time_minutes=45,
        variants=variants, layout=build_default_print_layout(variants),
    )
    return snapshot["variants"][0]["tasks"][0]


def test_printed_key_in_another_notation_gets_its_letter():
    printed = _printed_task(correct_answer="0.22", correct_answer_latex="$0{,}22$",
                            answer_options=[r"$\dfrac{11}{50}$", r"$\dfrac{1}{4}$", r"$\dfrac{1}{5}$"])

    assert [option["latex"] for option in printed["options"]].count(r"$\dfrac{11}{50}$") == 1
    assert len(printed["options"]) == 3
    letter = _teacher_choice_label(printed)
    keyed = next(option for option in printed["options"] if option["label"] == letter)
    assert keyed["latex"] == r"$\dfrac{11}{50}$"


def test_printed_choices_have_no_look_alike_of_the_key():
    options = print_choice_options({"task_id": "p2", "correct_answer": "2680", "correct_answer_latex": "$2680$",
                                    "answer_options": ["$2068$", "$2468$", "$20680$", "$2680$"]})

    assert [option["latex"] or option["value"] for option in options].count("$2680$") == 1


# ── the strict equivalence behind all of this ───────────────────────────────

@pytest.mark.parametrize("first, second", [
    ("0,22", r"$\dfrac{11}{50}$"), ("$0.5$", "1/2"), ("3 4/9", r"$3\dfrac{4}{9}$"),
    ("$[-4$; $2]$ и $[6$; $8)$", "$[-4; 2]$ и $[6; 8)$"), (r"$6\,000$", "6000"),
    (r"$\left(1; 2\right)$", "(1; 2)"), ("$0{,}78^{-0{,}7}$", "$0.78^{-0.7}$"),
    (r"$\log_{0{,}2} 0{,}7$", r"$\log_{0.2} 0.7$"),
    (r"$\sin{\alpha}$", r"$\sin \alpha$"),
    (r"$\dfrac{1}{\cos^{2}{\alpha}}$", r"$\dfrac{1}{\cos^{2} \alpha}$"),
    (r"$\log_{2}{x}$", r"$\log_{2} x$"),
    ("Если $a = -4$, то $x \\in [-a; 4)$.", "Если $a = -4$, то $x \\in [-a; 4)$"),
    ("$25, 9, -39, -52;$", "$25, 9, -39, -52$"),
])
def test_one_value_in_two_notations(first, second):
    assert answers_equivalent(first, second) and answers_equivalent(second, first)


@pytest.mark.parametrize("first, second", [
    ("1/3", "0,33"), ("3 4/9", "34/9"), ("(-∞; 2)", "(-∞; 2]"), ("x", "-x"), ("51\\%", "0,51"),
    ("2; 3", "3; 2"), ("", ""), (r"\sqrt{2}", "2"), ("да", "нет"), ("1, 2", "1.2"),
    (r"$\log_{0.2} 0.7$", r"$\log_{0.7} 0.2$"),
    (r"$\sin \alpha$", r"$\sin 2\alpha$"), (r"$\sin{x}$", r"$\sin{y}$"), ("1.", "1.5"),
    (r"$x \in (4; -a]$", r"$x \in (4; -a)$"),
])
def test_different_answers_stay_different(first, second):
    assert not answers_equivalent(first, second) and not answers_equivalent(second, first)


def test_exact_number_has_no_float_tolerance():
    assert exact_number("0,33") == Fraction(33, 100) != exact_number("1/3")
