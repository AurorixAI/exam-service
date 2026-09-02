"""Render one deterministic QA fixture for the ALGO printable-exam template.

This script never reads or writes the production database.  It exercises the
same immutable snapshot -> student PDF pipeline that the print-package API
uses, including choice bubbles, an open response and a separate answer sheet.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from src.domain.print_package import build_default_print_layout, build_print_snapshot
from src.domain.print_pdf import render_student_exam_pdf


def control_variants() -> list[dict]:
    return [
        {
            "variant_number": 1,
            "tasks": [
                {
                    "task_id": "quadratic-equation",
                    "skill_name": "Квадратные уравнения",
                    "question_text": "Решите уравнение.",
                    "question_latex": r"x^2 - 5x + 6 = 0",
                    "correct_answer": "2",
                    "correct_answer_latex": "2",
                    "answer_options": ["−1", "2", "3", "6"],
                    "answer_options_latex": ["-1", "2", "3", "6"],
                },
                {
                    "task_id": "function-domain",
                    "skill_name": "Область определения функции",
                    "question_text": "Укажите область определения функции.",
                    "question_latex": r"y = \frac{x + 1}{x^2 - 9}",
                    "correct_answer": "x ≠ −3, x ≠ 3",
                    "correct_answer_latex": r"x \ne -3,\ x \ne 3",
                    "answer_options": [
                        "x ≠ 0",
                        "x ≠ −3, x ≠ 3",
                        "x ≥ −3",
                        "x ∈ R",
                    ],
                    "answer_options_latex": [
                        r"x \ne 0",
                        r"x \ne -3,\ x \ne 3",
                        r"x \ge -3",
                        r"x \in \mathbb{R}",
                    ],
                },
                {
                    "task_id": "fraction-sum",
                    "skill_name": "Обыкновенные дроби",
                    "question_text": "Вычислите значение выражения и запишите ответ в листе ответов.",
                    "question_latex": r"\frac{3}{4} + \frac{5}{6}",
                    "correct_answer": "19/12",
                    "correct_answer_latex": r"\frac{19}{12}",
                    "distractor_meta": [],
                },
            ],
        }
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    variants = control_variants()
    layout = build_default_print_layout(variants)
    # Only the response kind is a teacher choice; the answer-sheet location and
    # the fixed 2/6 line geometry are canonicalised by build_print_snapshot.
    layout["variants"]["1"]["fraction-sum"]["response_kind"] = "open"
    snapshot = build_print_snapshot(
        exam_id="qa-offline-print-exam",
        title="Контрольная работа · Алгебра",
        subject="Математика",
        group_name="Математика-10",
        class_level=10,
        lesson_id="qa-lesson-2026-08-25",
        lesson_date="2026-08-25",
        total_time_minutes=45,
        variants=variants,
        layout=layout,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(render_student_exam_pdf(snapshot, 1))


if __name__ == "__main__":
    main()
