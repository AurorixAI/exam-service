"""Pure-domain tests for the immutable printed-exam contract.

These tests intentionally do not render a PDF.  They prove that teacher
settings only shape the package layout and cannot mutate an authored task.
The visual renderer is checked separately from a prepared fixture.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from src.domain.print_package import (
    DEFAULT_OPEN_ANSWER_LINES,
    DEFAULT_OPEN_WORK_LINES,
    PrintLayoutValidationError,
    build_default_print_layout,
    build_print_snapshot,
    print_choice_options,
    reconcile_print_layout,
    snapshot_sha256,
    validate_print_layout,
)


def _variants() -> list[dict]:
    return [
        {
            "variant_number": 1,
            "tasks": [
                {
                    "task_id": "quadratic-choice",
                    "skill_name": "Квадратные уравнения",
                    "question_text": "Решите уравнение.",
                    "question_latex": r"x^2 - 5x + 6 = 0",
                    "correct_answer": "2",
                    "correct_answer_latex": "2",
                    "answer_options": ["1", "2", "3", "6"],
                    "answer_options_latex": ["1", "2", "3", "6"],
                },
                {
                    "task_id": "fraction-open",
                    "skill_name": "Обыкновенные дроби",
                    "question_text": "Вычислите сумму.",
                    "question_latex": r"\frac{3}{4} + \frac{5}{6}",
                    "correct_answer": "19/12",
                    "correct_answer_latex": r"\frac{19}{12}",
                    "distractor_meta": [],
                },
            ],
        }
    ]


def test_default_layout_keeps_choice_and_open_tasks_distinct() -> None:
    layout = build_default_print_layout(_variants())
    choice = layout["variants"]["1"]["quadratic-choice"]
    open_task = layout["variants"]["1"]["fraction-open"]

    assert choice == {
        "response_kind": "choice",
        "answer_location": "answer_sheet",
        "answer_lines": 0,
        "work_lines": 0,
    }
    assert open_task["response_kind"] == "open"
    assert open_task["answer_location"] == "answer_sheet"
    assert open_task["answer_lines"] == DEFAULT_OPEN_ANSWER_LINES == 5
    assert open_task["work_lines"] == DEFAULT_OPEN_WORK_LINES == 11


def test_print_choices_deduplicate_equivalent_latex_without_losing_the_correct_answer() -> None:
    task = {
        "correct_answer": r"$\frac{1}{3m + 1}$",
        "correct_answer_latex": r"$\dfrac{1}{3m + 1}$",
        "answer_options": [
            r"$\dfrac{1}{3m + 1}$",
            r"$\frac{1}{3m + 1}$",
            "$1$",
        ],
        "answer_options_latex": [
            r"$\dfrac{1}{3m + 1}$",
            r"$\frac{1}{3m + 1}$",
            "$1$",
        ],
    }

    options = print_choice_options(task)
    # The list is deterministically shuffled per task, so equality is checked
    # as a set: the invariant here is de-duplication, not the printed order.
    # The merged option prints the author's notation and carries the key as
    # its value, which is how the teacher key finds its letter.
    assert sorted(option["latex"] for option in options) == sorted([
        r"$\dfrac{1}{3m + 1}$",
        "$1$",
    ])
    assert [option["value"] for option in options].count(task["correct_answer"]) == 1


def test_open_answer_uses_the_fixed_answer_sheet_without_changing_source_task() -> None:
    variants = _variants()
    original = deepcopy(variants)
    layout = build_default_print_layout(variants)
    # Legacy manual geometry is accepted at the API boundary, then reduced to
    # the one printable standard.  This cannot alter the authored task.
    layout["variants"]["1"]["fraction-open"].update({"answer_lines": 3, "work_lines": 8})

    snapshot = build_print_snapshot(
        exam_id="exam-print-001",
        title="Контрольная работа",
        subject="Математика",
        group_name="Математика-10",
        class_level=10,
        lesson_id="lesson-001",
        lesson_date="2026-08-25",
        total_time_minutes=45,
        variants=variants,
        layout=layout,
    )

    rendered_task = snapshot["variants"][0]["tasks"][1]
    assert rendered_task["response_kind"] == "open"
    assert rendered_task["answer_location"] == "answer_sheet"
    assert rendered_task["answer_lines"] == 5
    assert rendered_task["work_lines"] == 11
    assert variants == original


def test_answer_sheet_is_always_canonicalised_to_enabled() -> None:
    layout = build_default_print_layout(_variants())
    layout["answer_sheet_enabled"] = False
    layout["variants"]["1"]["fraction-open"]["answer_location"] = "answer_sheet"

    validated = validate_print_layout(_variants(), layout)
    assert validated["answer_sheet_enabled"] is True


def test_non_choice_task_cannot_be_forced_into_multiple_choice() -> None:
    layout = build_default_print_layout(_variants())
    layout["variants"]["1"]["fraction-open"].update({
        "response_kind": "choice",
        "answer_location": "answer_sheet",
        "answer_lines": 0,
        "work_lines": 0,
    })

    with pytest.raises(PrintLayoutValidationError, match="нельзя вывести как тест"):
        validate_print_layout(_variants(), layout)


def test_snapshot_hash_is_stable_and_changes_when_a_layout_changes() -> None:
    layout = build_default_print_layout(_variants())
    params = {
        "exam_id": "exam-print-001",
        "title": "Контрольная работа",
        "subject": "Математика",
        "group_name": "Математика-10",
        "class_level": 10,
        "lesson_id": "lesson-001",
        "lesson_date": "2026-08-25",
        "total_time_minutes": 45,
        "variants": _variants(),
    }
    first = build_print_snapshot(layout=layout, **params)
    second = build_print_snapshot(layout=deepcopy(layout), **params)
    assert snapshot_sha256(first) == snapshot_sha256(second)

    changed = deepcopy(layout)
    changed["variants"]["1"]["quadratic-choice"]["response_kind"] = "open"
    changed_snapshot = build_print_snapshot(layout=changed, **params)
    assert snapshot_sha256(first) != snapshot_sha256(changed_snapshot)


def test_replacing_one_task_preserves_other_print_settings_and_defaults_new_task() -> None:
    original = _variants()
    replacement_variants = deepcopy(original)
    replacement_variants[0]["tasks"][0] = {
        "task_id": "new-choice",
        "skill_name": "Квадратные уравнения",
        "question_text": "Найдите корни уравнения.",
        "correct_answer": "3",
        "answer_options": ["1", "2", "3", "4"],
    }
    previous_layout = build_default_print_layout(original)
    previous_layout["answer_sheet_enabled"] = False
    previous_layout["variants"]["1"]["fraction-open"].update({
        "response_kind": "open",
        "answer_location": "inline",
        "answer_lines": 5,
        "work_lines": 12,
    })

    reconciled = reconcile_print_layout(replacement_variants, previous_layout)

    assert reconciled["answer_sheet_enabled"] is True
    assert reconciled["variants"]["1"]["fraction-open"] == {
        "response_kind": "open",
        "answer_location": "answer_sheet",
        "answer_lines": DEFAULT_OPEN_ANSWER_LINES,
        "work_lines": DEFAULT_OPEN_WORK_LINES,
    }
    assert reconciled["variants"]["1"]["new-choice"] == {
        "response_kind": "choice",
        "answer_location": "answer_sheet",
        "answer_lines": 0,
        "work_lines": 0,
    }
    assert "quadratic-choice" not in reconciled["variants"]["1"]
    assert original == _variants()


def test_print_options_shuffle_deterministically_per_task() -> None:
    task = {
        "task_id": "shuffle-determinism",
        "skill_name": "Тест",
        "question_text": "Выберите значение.",
        "correct_answer": "2",
        "answer_options": ["1", "3", "6"],
    }
    first = [option["value"] for option in print_choice_options(task)]
    second = [option["value"] for option in print_choice_options(task)]
    assert first == second


def test_print_options_do_not_put_the_correct_answer_on_the_same_letter() -> None:
    labels = []
    for index in range(10):
        task = {
            "task_id": f"letter-probe-{index}",
            "skill_name": "Тест",
            "question_text": "Выберите значение.",
            "correct_answer": "20",
            # Generated variants normally store only distractors: the correct
            # answer is merged in by print_choice_options before shuffling.
            "answer_options": ["10", "30", "40"],
        }
        options = print_choice_options(task)
        # Labels are attached later, in build_print_snapshot (A, B, C...);
        # here the printed letter is just the option's position.
        labels.append(
            chr(65 + next(i for i, option in enumerate(options) if option["value"] == "20"))
        )
    assert len(set(labels)) > 1


def test_snapshot_groups_tasks_by_the_same_sections_as_the_draft_editor() -> None:
    """A raw A/B/A order must print as section A, then section B."""
    variants = [
        {
            "variant_number": 1,
            "tasks": [
                {"task_id": "a-1", "skill_name": "Раздел A", "question_text": "A1", "correct_answer": "1"},
                {"task_id": "b-1", "skill_name": "Раздел B", "question_text": "B1", "correct_answer": "2"},
                {"task_id": "a-2", "skill_name": "Раздел A", "question_text": "A2", "correct_answer": "3"},
                {"task_id": "a-3", "skill_name": "Раздел A", "question_text": "A3", "correct_answer": "4"},
            ],
        }
    ]
    snapshot = build_print_snapshot(
        exam_id="sectioned-print",
        title="Контрольная работа",
        subject="Математика",
        group_name="Математика-9",
        class_level=9,
        lesson_id="lesson-sections",
        lesson_date="2026-08-27",
        total_time_minutes=45,
        variants=variants,
        layout=build_default_print_layout(variants),
    )

    variant = snapshot["variants"][0]
    assert [(section["ordinal"], section["name"]) for section in variant["sections"]] == [
        (1, "Раздел A"),
        (2, "Раздел B"),
    ]
    assert [task["task_id"] for task in variant["sections"][0]["tasks"]] == ["a-1", "a-2", "a-3"]
    assert [task["task_id"] for task in variant["tasks"]] == ["a-1", "a-2", "a-3", "b-1"]
    assert [task["ordinal"] for task in variant["tasks"]] == [1, 2, 3, 4]
