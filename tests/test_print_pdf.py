"""Regression tests for the server-side printable PDF renderer."""
from __future__ import annotations

import base64
from copy import deepcopy
from io import BytesIO

import pytest
from reportlab.lib.units import mm

from src.domain.print_package import build_default_print_layout, build_print_snapshot
from src.domain.print_pdf import (
    BOTTOM,
    CONTENT_FOOTER_CLEARANCE,
    CONTENT_WIDTH,
    TASK_BADGE_FONT_SIZE,
    TASK_BADGE_RADIUS,
    _MixedTextCell,
    _PdfDocument,
    _choice_table,
    _choice_text_cell,
    _choice_option_formula,
    _ensure_fonts,
    _formula_for_math_renderer,
    _is_plain_math_value,
    _is_text_choice_value,
    _latex_to_print_text,
    _mixed_inline_rows,
    _render_math_artifact,
    _split_print_parts,
    _styles,
    _task_badge_text_baseline,
    _teacher_choice_label,
    _teacher_key_value_offset,
    render_student_exam_pdf,
    render_teacher_key_pdf,
)


def _print_snapshot() -> dict:
    variants = [
        {
            "variant_number": 1,
            "tasks": [
                {
                    "task_id": "fraction-choice",
                    "skill_name": "Обыкновенные дроби",
                    "question_text": "Вычислите значение выражения.",
                    "question_latex": r"$\frac{3}{4} + \sqrt{2x^2} = \frac{5}{6}$",
                    "correct_answer": "3",
                    "correct_answer_latex": r"\frac{9}{3}",
                    "answer_options": ["1", "2", "3", "9/3"],
                    "answer_options_latex": ["1", "2", "3", r"\frac{9}{3}"],
                },
                {
                    "task_id": "open-root",
                    "skill_name": "Степени и корни",
                    "question_text": "Упростите выражение.",
                    "question_latex": r"$\left(\frac{1}{x}\right)^2$",
                    "correct_answer": "1/x²",
                    "correct_answer_latex": r"\frac{1}{x^2}",
                    "distractor_meta": [],
                },
            ],
        }
    ]
    return build_print_snapshot(
        exam_id="print-pdf-qa",
        title="Контрольная работа",
        subject="Математика",
        group_name="Математика-9",
        class_level=9,
        lesson_id="lesson-pdf-qa",
        lesson_date="2026-08-25",
        total_time_minutes=45,
        variants=variants,
        layout=build_default_print_layout(variants),
    )


def test_embedded_task_figure_is_drawn_into_student_pdf() -> None:
    """A protected package keeps its task figure instead of dropping it."""
    pillow = pytest.importorskip("PIL.Image")
    snapshot = _print_snapshot()
    image = pillow.new("RGB", (2, 2), color=(40, 91, 234))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    # Production snapshots receive the same field from the first-party figures
    # service, together with a content checksum.
    snapshot["variants"][0]["tasks"][0]["question_image"] = {
        "content_type": "image/png",
        "data_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
        "sha256": "test",
    }

    pdf = render_student_exam_pdf(snapshot, 1)
    assert pdf.startswith(b"%PDF")
    assert b"/Image" in pdf


def test_standalone_verified_latex_is_typeset_instead_of_flattened() -> None:
    pytest.importorskip("matplotlib")

    formula = r"\frac{9}{3}"
    assert _split_print_parts(formula) == [("math", formula)]
    artifact = _render_math_artifact(formula, 10.4, "#101B35")
    assert artifact is not None
    assert artifact.png.startswith(b"\x89PNG\r\n\x1a\n")
    assert artifact.width > 0
    assert artifact.height > 0


def test_undelimited_formula_lines_are_typeset_inside_mixed_prose() -> None:
    pytest.importorskip("matplotlib")

    value = "Вычислите значение выражения.\n\\frac{3}{4} + \\frac{5}{6}"
    assert _split_print_parts(value) == [
        ("text", "Вычислите значение выражения."),
        ("math", "\\frac{3}{4} + \\frac{5}{6}"),
    ]
    assert _render_math_artifact("\\frac{3}{4} + \\frac{5}{6}", 12.0, "#101B35") is not None


def test_plain_answers_keep_the_document_font_instead_of_mathtext() -> None:
    """A bare key answer such as ``7`` must not become italic or oversized."""
    assert _split_print_parts("7") == [("text", "7")]
    assert _split_print_parts("$7$") == [("text", "7")]
    assert _split_print_parts("$45$") == [("text", "45")]
    assert _split_print_parts("(2; -1); (-2; 1)") == [("text", "(2; -1); (-2; 1)")]
    assert _split_print_parts("$(-2; 2), (2; -2)$") == [("text", "(-2; 2), (2; -2)")]
    assert _split_print_parts("x = False; x = False") == [("text", "x = False; x = False")]


def test_choice_formula_fragments_render_as_one_clean_coordinate_list() -> None:
    """Separated TeX fragments must not degrade fractions to double brackets."""
    value = r"$(2; 1)$; $\left(\frac{2}{3}; 3\right)$; $\left(-\frac{2}{3}; -3\right)$"
    assert _choice_option_formula(value) == r"(2; 1); \left(\frac{2}{3}; 3\right); \left(-\frac{2}{3}; -3\right)"


def test_short_relation_aliases_render_as_the_standard_math_command() -> None:
    pytest.importorskip("matplotlib")

    formula = _formula_for_math_renderer(r"$a \le 0$")
    assert formula == r"a \le 0"
    assert _render_math_artifact(formula, 9.8, "#101B35") is not None


def test_relation_alias_normalisation_never_corrupts_left_delimiters() -> None:
    pytest.importorskip("matplotlib")

    formula = _formula_for_math_renderer(r"$y = \left| x - \frac{1}{2} \right|$")
    assert formula == r"y = \left| x - \frac{1}{2} \right|"
    assert _render_math_artifact(formula, 12.0, "#101B35") is not None
    assert _latex_to_print_text(r"Да, $T = \frac{1}{2}$") == "Да, T = ½"


def test_long_choice_prose_uses_a_compact_readable_style() -> None:
    doc = _PdfDocument("long-choice")
    short = _choice_text_cell(doc, "Короткий ответ.", doc.styles["option"])
    long = _choice_text_cell(
        doc,
        "Предположим противное и последовательно применим определение строгой монотонности "
        "к каждой паре значений функции, чтобы получить противоречие с исходным равенством.",
        doc.styles["option"],
    )

    assert long.style.fontSize < short.style.fontSize
    assert long.style.leading < short.style.leading


def test_mixed_choice_cells_keep_inline_latex_as_math() -> None:
    doc = _PdfDocument("mixed-choice")
    cell = _choice_text_cell(
        doc,
        r"Из $f(x_{1}) = f(x_{2})$ следует $x_{1} = x_{2}$.",
        doc.styles["option"],
    )

    assert isinstance(cell, _MixedTextCell)
    _, height = cell.wrap(CONTENT_WIDTH - 38 * mm, 500)
    assert height > 0


def test_plain_choice_scalars_and_coordinate_lists_skip_mathtext() -> None:
    """Choice rows must not make a plain ``1`` or coordinates oversized."""
    assert _is_plain_math_value("$1$") is True
    assert _is_plain_math_value("$(2; -1); (-2; 1)$") is True


def test_simple_root_coordinate_choices_use_the_document_font() -> None:
    """Root coordinates must not use a different raster typeface per row."""
    value = r"$(2 + \sqrt{7}; 2 - \sqrt{7}), (-2; 3)$"
    assert _latex_to_print_text(r"\sqrt{7}") == "√7"
    assert _is_text_choice_value(value) is True
    assert _is_text_choice_value(r"$\frac{1}{3m + 1}$") is False


def test_standalone_parameters_use_document_fonts() -> None:
    """Parameters in prose must not become misaligned MathText images."""
    assert _split_print_parts("При каком значении $\\alpha$?") == [
        ("text", "При каком значении "),
        ("text", "α"),
        ("text", "?"),
    ]
    assert _split_print_parts("При каком значении $\\alpha$ окружность?") == [
        ("text", "При каком значении "),
        ("text", "α"),
        ("text", " окружность?"),
    ]
    # Some imported task banks store the actual Unicode symbol instead of the
    # TeX command. It must retain the document font as well.
    assert _split_print_parts("При каком значении $α$?") == [
        ("text", "При каком значении "),
        ("text", "α"),
        ("text", "?"),
    ]
    # A Latin parameter is italic but remains live document text: MathText
    # rendered it as an oversized, baseline-shifted image in the PDF.
    assert _split_print_parts("При каком значении $a$?") == [
        ("text", "При каком значении "),
        ("italic", "a"),
        ("text", "?"),
    ]


def test_plain_inline_values_keep_word_boundaries() -> None:
    """A scalar/Greek token between prose fragments must retain both spaces."""
    rows = _mixed_inline_rows(
        "При каком значении $\\alpha$ окружность равна $45$.",
        _styles()["task"],
        width=500,
        formula_size=12.0,
        formula_color="#101B35",
    )
    units = [unit for _, row in rows for unit in row]
    gaps = {str(payload): gap for _, payload, _, _, gap in units}
    assert gaps["α"] > 0
    assert gaps["окружность"] > 0
    assert gaps["45"] > 0


def test_math_artifacts_have_transparent_backgrounds() -> None:
    """Formula PNGs may sit on a tinted option table without white boxes."""
    pillow = pytest.importorskip("PIL.Image")
    pytest.importorskip("matplotlib")

    artifact = _render_math_artifact(r"\frac{1}{3m + 1}", 9.8, "#101B35")
    assert artifact is not None
    alpha = pillow.open(BytesIO(artifact.png)).convert("RGBA").getchannel("A")
    assert alpha.getextrema()[0] == 0


def test_task_badge_baseline_is_centred_from_font_metrics() -> None:
    """All task ordinals use one geometry, rather than a lower-edge offset."""
    _, bold = _ensure_fonts()
    baseline = _task_badge_text_baseline(400, bold)
    from reportlab.pdfbase import pdfmetrics

    visual_centre = baseline + (
        pdfmetrics.getAscent(bold, TASK_BADGE_FONT_SIZE)
        + pdfmetrics.getDescent(bold, TASK_BADGE_FONT_SIZE)
    ) / 2
    assert visual_centre == pytest.approx(400 - TASK_BADGE_RADIUS)


def test_compact_page_reserve_keeps_a_complete_task_above_the_footer() -> None:
    """The renderer may use the safe lower page area before starting a page."""
    doc = _PdfDocument("page-reserve")
    doc.y = BOTTOM + CONTENT_FOOTER_CLEARANCE + 20

    doc.ensure(20)
    assert doc.page == 1

    doc.ensure(20.1)
    assert doc.page == 2


def test_teacher_key_uses_regular_weight_for_every_answer_value() -> None:
    """Green colour identifies answers; bold weight must not vary by value."""
    styles = _styles()
    assert styles["key"].fontName == styles["option"].fontName


def test_cases_environment_is_typeset_without_leaking_tex_syntax() -> None:
    pytest.importorskip("matplotlib")

    cases = r"\begin{cases} x^2 - y^2 = 8,\\ x - y = 2 \end{cases}"
    assert _split_print_parts(cases) == [("math", cases)]
    assert _render_math_artifact(cases, 12.0, "#101B35") is not None


def test_mixed_prose_and_formula_keep_one_trailing_fragment() -> None:
    """The renderer must not duplicate text after an inline formula."""
    assert _split_print_parts("Решите $x^2 = 9$ и запишите ответ.") == [
        ("text", "Решите "),
        ("math", "$x^2 = 9$"),
        ("text", " и запишите ответ."),
    ]


def test_printed_choice_labels_have_no_bullets_or_trailing_dots() -> None:
    """Student papers use clean A/B/C labels; bubbles belong only to answer sheets."""
    doc = _PdfDocument("choice-labels")
    table = _choice_table(
        doc,
        [
            {"label": "A.", "value": "1", "latex": "1"},
            {"label": "B.", "value": "2", "latex": "2"},
        ],
        key=False,
        correct_answer="1",
    )

    assert table is not None
    labels = [row[0].getPlainText() for row in table._cellvalues]
    assert labels == ["A", "B"]


def test_teacher_key_uses_the_printed_letter_for_a_choice_task() -> None:
    task = {
        "correct_answer": r"$\frac{1}{3m + 1}$",
        "correct_answer_latex": r"$\dfrac{1}{3m + 1}$",
        "options": [
            {"label": "A.", "value": "$1$", "latex": "$1$"},
            {"label": "B.", "value": r"$\frac{1}{3m + 1}$", "latex": r"$\frac{1}{3m + 1}$"},
        ],
    }

    assert _teacher_choice_label(task) == "B"


def test_teacher_key_value_is_centred_against_its_ordinal_badge() -> None:
    doc = _PdfDocument("teacher-key-centre")

    assert _teacher_key_value_offset(doc, "A") == pytest.approx(
        TASK_BADGE_RADIUS - doc.styles["key"].leading / 2
    )


def test_pdf_rendering_never_mutates_the_prepared_exam_snapshot() -> None:
    """PDF output is a pure presentation step over an immutable print snapshot."""
    pytest.importorskip("matplotlib")

    snapshot = _print_snapshot()
    before = deepcopy(snapshot)

    render_student_exam_pdf(snapshot, 1)
    render_teacher_key_pdf(snapshot, 1)

    assert snapshot == before


def test_student_and_teacher_print_pdfs_are_valid_a4_documents() -> None:
    pytest.importorskip("matplotlib")

    snapshot = _print_snapshot()
    student_pdf = render_student_exam_pdf(snapshot, 1)
    key_pdf = render_teacher_key_pdf(snapshot, 1)

    assert student_pdf.startswith(b"%PDF-")
    assert key_pdf.startswith(b"%PDF-")
    # A student paper and a teacher key have intentionally different content.
    assert student_pdf != key_pdf
