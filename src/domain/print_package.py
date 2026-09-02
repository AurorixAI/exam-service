"""Canonical data model for immutable printed exam materials.

This module is deliberately framework-free.  It is used before a package is
written and again by the PDF renderer, which gives one deterministic source of
truth for the student paper and the teacher key.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import random
from typing import Any


PRINT_LAYOUT_SCHEMA_VERSION = 1
MAX_CHOICE_OPTIONS = 6
# A handwritten response must have enough room for a complete short solution
# on both the task page and the separate answer sheet.
DEFAULT_OPEN_ANSWER_LINES = 5
DEFAULT_OPEN_WORK_LINES = 11


class PrintLayoutValidationError(ValueError):
    """A teacher-provided print configuration cannot represent the task."""


def _string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _task_id(task: dict[str, Any]) -> str:
    value = _string(task.get("task_id") or task.get("id"))
    if not value:
        raise PrintLayoutValidationError("У задания отсутствует идентификатор для печатного бланка")
    return value


def _section_name(task: dict[str, Any]) -> str:
    """Return the same logical task group that the draft editor displays.

    The editable draft groups tasks by their visible skill name (and falls
    back to the skill identifier).  A printed package must preserve that
    grouping instead of following an incidental source ordering such as
    A / B / A, which would split one section across several pages.
    """
    return _string(task.get("skill_name") or task.get("skill_id")) or "Без категории"


def _distractor_value(item: Any) -> tuple[str, str]:
    if not isinstance(item, dict):
        return "", ""
    value = _string(item.get("value") or item.get("text") or item.get("content"))
    latex = _string(item.get("value_latex") or item.get("latex"))
    return value, latex


def choice_option_identity(value: str, latex: str = "") -> str:
    """Return a presentation-independent identity for one answer option.

    Authoring tools may describe the same fraction with ``\\frac`` or
    ``\\dfrac``.  They are identical answers, so a printed choice list must
    not show them as separate variants simply because their TeX wrappers
    differ.
    """
    source = _string(latex or value)
    source = source.removeprefix("$$").removesuffix("$$")
    source = source.removeprefix("$").removesuffix("$")
    source = source.removeprefix(r"\(").removesuffix(r"\)")
    source = source.replace(r"\dfrac", r"\frac").replace(r"\tfrac", r"\frac")
    source = source.replace(r"\left", "").replace(r"\right", "")
    return "".join(source.split())


def print_choice_options(task: dict[str, Any]) -> list[dict[str, str]]:
    """Return a stable, de-duplicated choice list for a printable task.

    Online delivery intentionally shuffles a student's options.  Printed
    materials must instead have a fixed, reviewable order that becomes part of
    the package snapshot and teacher key.  The order is still shuffled once,
    deterministically, seeded by the task id: a generated variant normally
    stores only distractors, so without the shuffle the merged correct answer
    would land on letter A in every task of every variant.
    """
    correct = _string(task.get("correct_answer"))
    correct_latex = _string(task.get("correct_answer_latex"))
    correct_identity = choice_option_identity(correct, correct_latex)
    candidates: list[tuple[str, str]] = []

    raw_options = task.get("answer_options")
    if isinstance(raw_options, list):
        raw_latex = task.get("answer_options_latex")
        for index, option in enumerate(raw_options):
            if isinstance(option, dict):
                value = _string(option.get("value") or option.get("text") or option.get("content"))
                latex = _string(option.get("value_latex") or option.get("latex"))
            else:
                value = _string(option)
                latex = ""
            if not latex and isinstance(raw_latex, list) and index < len(raw_latex):
                latex = _string(raw_latex[index])
            candidates.append((value, latex))

    if not candidates:
        candidates.extend(_distractor_value(item) for item in (task.get("distractor_meta") or []))

    if correct:
        # Keep the authored option order if the correct answer is already in
        # it; otherwise put the answer first instead of quietly dropping it.
        if not any(
            choice_option_identity(value, latex) == correct_identity
            for value, latex in candidates
        ):
            candidates.insert(0, (correct, correct_latex))

    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for value, latex in candidates:
        identity = choice_option_identity(value, latex)
        if not value or not identity or identity in seen:
            continue
        seen.add(identity)
        options.append({"value": value, "latex": latex or (correct_latex if value == correct else "")})

    # Same input -> same order (the snapshot hash stays stable), but different
    # tasks -> different orders, so the correct letter is not a pattern.
    seed_source = _string(task.get("task_id") or task.get("id")) or _string(correct)
    random.Random(seed_source).shuffle(options)
    return options


def can_render_as_choice(task: dict[str, Any]) -> bool:
    return 2 <= len(print_choice_options(task)) <= MAX_CHOICE_OPTIONS


def _default_task_layout(task: dict[str, Any]) -> dict[str, Any]:
    if can_render_as_choice(task):
        return {
            "response_kind": "choice",
            "answer_location": "answer_sheet",
            "answer_lines": 0,
            "work_lines": 0,
        }
    return {
        "response_kind": "open",
        "answer_location": "answer_sheet",
        "answer_lines": DEFAULT_OPEN_ANSWER_LINES,
        "work_lines": DEFAULT_OPEN_WORK_LINES,
    }


def build_default_print_layout(variants: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a complete editable layout without changing a task itself."""
    by_variant: dict[str, dict[str, dict[str, Any]]] = {}
    for variant in variants or []:
        number = int(variant.get("variant_number") or 1)
        tasks = variant.get("tasks") or []
        task_layouts: dict[str, dict[str, Any]] = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = _task_id(task)
            if task_id in task_layouts:
                raise PrintLayoutValidationError(
                    f"В варианте №{number} два задания имеют идентификатор {task_id}"
                )
            task_layouts[task_id] = _default_task_layout(task)
        if str(number) in by_variant:
            raise PrintLayoutValidationError(f"Дублирующийся номер варианта: {number}")
        by_variant[str(number)] = task_layouts
    return {
        "schema_version": PRINT_LAYOUT_SCHEMA_VERSION,
        "answer_sheet_enabled": True,
        "variants": by_variant,
    }


def reconcile_print_layout(
    variants: list[dict[str, Any]],
    previous_layout: Any,
) -> dict[str, Any]:
    """Keep valid print choices when a draft task is replaced.

    A print layout is keyed by task id, while a draft replacement intentionally
    changes one of those ids.  Reusing the old layout verbatim would make the
    next preview fail validation; regenerating all settings would discard the
    teacher's choices for every unchanged task.  This function does neither:
    it builds the exact current structure first, then overlays only settings
    that still belong to the same variant and task.

    The return value is always canonical for ``variants`` and never edits an
    authored task, answer, distractor, or explanation.
    """
    reconciled = build_default_print_layout(variants)
    if not isinstance(previous_layout, dict):
        return reconciled

    previous_variants = previous_layout.get("variants")
    source_tasks_by_variant = {
        str(int(variant.get("variant_number") or 1)): {
            _task_id(task): task
            for task in (variant.get("tasks") or [])
            if isinstance(task, dict)
        }
        for variant in variants or []
    }
    if isinstance(previous_variants, dict):
        for variant_key, task_layouts in reconciled["variants"].items():
            previous_tasks = previous_variants.get(variant_key)
            if not isinstance(previous_tasks, dict):
                continue
            for task_id in task_layouts:
                previous_task_layout = previous_tasks.get(task_id)
                if isinstance(previous_task_layout, dict):
                    # Only the response kind remains a teacher choice.  The
                    # answer sheet and mathematical work grid are fixed
                    # package standards, so historical line counts and
                    # locations cannot destabilise a regenerated draft.
                    response_kind = previous_task_layout.get("response_kind")
                    task = source_tasks_by_variant[variant_key][task_id]
                    if response_kind == "open":
                        task_layouts[task_id] = {
                            "response_kind": "open",
                            "answer_location": "answer_sheet",
                            "answer_lines": DEFAULT_OPEN_ANSWER_LINES,
                            "work_lines": DEFAULT_OPEN_WORK_LINES,
                        }
                    elif response_kind == "choice" and can_render_as_choice(task):
                        task_layouts[task_id] = {
                            "response_kind": "choice",
                            "answer_location": "answer_sheet",
                            "answer_lines": 0,
                            "work_lines": 0,
                        }

    try:
        return validate_print_layout(variants, reconciled)
    except PrintLayoutValidationError:
        # A legacy or manually malformed setting must never prevent a teacher
        # from replacing a draft task.  Defaults remain deterministic and the
        # raw task content stays untouched.
        return build_default_print_layout(variants)


def validate_print_layout(variants: list[dict[str, Any]], layout: Any) -> dict[str, Any]:
    """Validate and canonicalise the full print layout received from the UI."""
    if not isinstance(layout, dict):
        raise PrintLayoutValidationError("Нужна полная структура печатного бланка")
    if layout.get("schema_version", PRINT_LAYOUT_SCHEMA_VERSION) != PRINT_LAYOUT_SCHEMA_VERSION:
        raise PrintLayoutValidationError("Неподдерживаемая версия структуры печатного бланка")
    if not isinstance(layout.get("answer_sheet_enabled", True), bool):
        raise PrintLayoutValidationError("Параметр листа ответов должен быть логическим")
    source_variants = layout.get("variants")
    if not isinstance(source_variants, dict):
        raise PrintLayoutValidationError("Не найдены настройки вариантов для печати")

    canonical = build_default_print_layout(variants)
    # Every printed package contains one answer sheet.  Accept legacy input
    # below for backwards compatibility, but never permit a draft to remove
    # the answer sheet or introduce a per-task inline answer area.
    canonical["answer_sheet_enabled"] = True

    expected_variant_keys = set(canonical["variants"])
    if set(str(key) for key in source_variants) != expected_variant_keys:
        raise PrintLayoutValidationError("Настройки должны быть заданы для каждого варианта экзамена")

    tasks_by_variant: dict[str, dict[str, dict[str, Any]]] = {}
    for variant in variants or []:
        variant_key = str(int(variant.get("variant_number") or 1))
        tasks_by_variant[variant_key] = {
            _task_id(task): task
            for task in (variant.get("tasks") or [])
            if isinstance(task, dict)
        }
    for variant_key, expected_tasks in canonical["variants"].items():
        requested = source_variants.get(variant_key)
        if not isinstance(requested, dict) or set(requested) != set(expected_tasks):
            raise PrintLayoutValidationError(f"В варианте №{variant_key} должны быть настроены все задания")
        for task_id, task_layout in requested.items():
            if not isinstance(task_layout, dict):
                raise PrintLayoutValidationError(f"Некорректная настройка задания {task_id}")
            response_kind = task_layout.get("response_kind")
            if response_kind not in {"choice", "open"}:
                raise PrintLayoutValidationError(f"Выберите тип ответа для задания {task_id}")
            answer_location = task_layout.get("answer_location", "answer_sheet")
            if answer_location not in {"inline", "answer_sheet"}:
                raise PrintLayoutValidationError(
                    f"Выберите место для ответа в задании {task_id}"
                )
            task = tasks_by_variant[variant_key][task_id]
            if response_kind == "choice" and not can_render_as_choice(task):
                raise PrintLayoutValidationError(
                    f"Задание {task_id} нельзя вывести как тест: требуется от 2 до {MAX_CHOICE_OPTIONS} вариантов"
                )
            if response_kind == "choice":
                answer_location = "answer_sheet"
                answer_lines = 0
                work_lines = 0
            else:
                answer_location = "answer_sheet"
                answer_lines = DEFAULT_OPEN_ANSWER_LINES
                work_lines = DEFAULT_OPEN_WORK_LINES
            expected_tasks[task_id] = {
                "response_kind": response_kind,
                "answer_location": answer_location,
                "answer_lines": answer_lines,
                "work_lines": work_lines,
            }
    return canonical


def build_print_snapshot(
    *,
    exam_id: str,
    title: str,
    subject: str,
    group_name: str | None,
    class_level: int | None,
    lesson_id: str,
    lesson_date: str,
    total_time_minutes: int,
    variants: list[dict[str, Any]],
    layout: dict[str, Any],
) -> dict[str, Any]:
    """Build the complete, immutable source for student PDFs and answer key."""
    layout = validate_print_layout(variants, layout)
    snapshot_variants: list[dict[str, Any]] = []
    for variant in variants or []:
        number = int(variant.get("variant_number") or 1)
        variant_layout = layout["variants"][str(number)]
        snapshot_tasks: list[dict[str, Any]] = []
        grouped_tasks: dict[str, list[dict[str, Any]]] = {}
        for raw_task in variant.get("tasks") or []:
            if not isinstance(raw_task, dict):
                continue
            task_id = _task_id(raw_task)
            response_layout = variant_layout[task_id]
            options = print_choice_options(raw_task) if response_layout["response_kind"] == "choice" else []
            section_name = _section_name(raw_task)
            section_tasks = grouped_tasks.setdefault(section_name, [])
            section_tasks.append({
                "task_id": task_id,
                # Set after grouping.  This intentionally mirrors the order
                # of the section cards in the source draft, not the raw task
                # record order.
                "ordinal": 0,
                "skill_name": _string(raw_task.get("skill_name")),
                "question_text": _string(raw_task.get("question_text") or raw_task.get("text")),
                "question_latex": _string(raw_task.get("question_latex")),
                "question_image_url": _string(raw_task.get("question_image_url")),
                "response_kind": response_layout["response_kind"],
                "answer_location": response_layout["answer_location"],
                "answer_lines": response_layout["answer_lines"],
                "work_lines": response_layout["work_lines"],
                "options": [
                    {"label": chr(65 + index), **option}
                    for index, option in enumerate(options)
                ],
                # Kept only inside the protected package, never in the student
                # PDF payload or a public API response.
                "correct_answer": _string(raw_task.get("correct_answer")),
                "correct_answer_latex": _string(raw_task.get("correct_answer_latex")),
                "points": 10,
            })
        snapshot_sections: list[dict[str, Any]] = []
        for section_ordinal, (section_name, section_tasks) in enumerate(grouped_tasks.items(), start=1):
            for task in section_tasks:
                task["ordinal"] = len(snapshot_tasks) + 1
                snapshot_tasks.append(task)
            snapshot_sections.append({
                "ordinal": section_ordinal,
                "name": section_name,
                "tasks": section_tasks,
            })
        snapshot_variants.append({
            "variant_number": number,
            # The flat list remains the canonical source for answer sheets
            # and for backwards compatibility with prepared packages.
            "tasks": snapshot_tasks,
            "sections": snapshot_sections,
        })

    return {
        "schema_version": 1,
        "exam": {
            "id": exam_id,
            "title": title.strip(),
            "subject": subject.strip() or "Математика",
            "group_name": group_name,
            "class_level": class_level,
            "lesson_id": lesson_id,
            "lesson_date": lesson_date,
            "total_time_minutes": total_time_minutes,
        },
        "layout": deepcopy(layout),
        "variants": snapshot_variants,
    }


def snapshot_sha256(snapshot: dict[str, Any]) -> str:
    """Stable SHA-256 for auditing and testable immutability guarantees."""
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()
