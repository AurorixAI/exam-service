"""
ALGO V1 — LLM Templatizer
src/exam_builder/llm_templatizer.py

Шаблонизация через Gemini Flash: переводит конкретную задачу
(текст + LaTeX + ответ) в параметрический шаблон TaskTemplate.

Вызывается ТОЛЬКО для числовых задач (exact_number, decimal, fraction)
и ТОЛЬКО при генерации вариантов (не при оцифровке).

Пример:
  Вход:  «Вычислите 15 + 27»  →  ответ 42
  Выход: TaskTemplate(
           text_template = "Вычислите {a} + {b}",
           answer_formula_text = "a + b",
           params = {a: ParamInfo(15,1,100), b: ParamInfo(27,1,100)},
           ...
         )

SymPy используется ТОЛЬКО для верификации: подставляем оригинальные
значения в формулу и проверяем, что результат совпадает с ответом.
"""

from __future__ import annotations

import logging
import math
import string
from typing import Any, Dict, Optional

from src.domain.template_models import ParamInfo, TaskTemplate

log = logging.getLogger("llm_templatizer")

# ─── Типы задач, для которых вызывается LLM ───
TEMPLATABLE_TYPES = {"exact_number", "decimal", "fraction"}

# ─── Имена параметров (по порядку) ───
_PARAM_LETTERS = list(string.ascii_lowercase[:10])  # a..j


# ═══════════════════════════════════════════════════════════════════════════
# Prompt
# ═══════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """\
Ты — математический шаблонизатор. Тебе дана конкретная математическая задача 
с числовым ответом. Твоя задача — заменить числовые значения параметрами 
(буквами a, b, c, ...) и вывести формулу ответа.

ПРАВИЛА:
1. Замени ВСЕ ключевые числовые значения на буквы (a, b, c, ...).
   НЕ заменяй: степени, порядковые номера, фиксированные константы (π, e).
2. Дай формулу ответа как Python-выражение через эти параметры.
   Допустимые операции: +, -, *, /, **, sqrt(), abs(), math.gcd()
3. Выведи constraints (ограничения) — условия на параметры, 
   при которых задача корректна (напр. "a != 0", "b > 0").
4. Для каждого параметра укажи разумный диапазон (min, max) 
   и тип (int или float). Диапазон должен давать «красивые» ответы.
5. Определи тип задачи: arithmetic, linear, quadratic, fraction, 
   geometry, logarithm, trigonometry, system, inequality, 
   power_root, derivative, integral, limit.

Верни JSON строго в формате:
{
  "task_type": "arithmetic",
  "text_template": "Вычислите {a} + {b}",
  "params": {
    "a": {"original_value": 15, "min": 1, "max": 100, "type": "int", "exclude": []},
    "b": {"original_value": 27, "min": 1, "max": 100, "type": "int", "exclude": []}
  },
  "answer_formula": "a + b",
  "constraints": ["a > 0", "b > 0"]
}
"""


def _build_prompt(
    text: str,
    latex: str,
    answer: str,
    answer_type: str,
) -> str:
    """Строит полный промпт для Gemini."""
    parts = [_SYSTEM_PROMPT, "\n--- ЗАДАЧА ---"]
    parts.append(f"Текст: {text}")
    if latex:
        parts.append(f"LaTeX: {latex}")
    parts.append(f"Правильный ответ: {answer}")
    parts.append(f"Тип ответа: {answer_type}")
    parts.append("\nВерни JSON.")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# Verification via SymPy
# ═══════════════════════════════════════════════════════════════════════════


def _verify_formula(
    formula_text: str,
    original_values: Dict[str, Any],
    expected_answer: float,
) -> bool:
    """Подставляем оригинальные значения в формулу — должно совпасть с ответом."""
    try:
        from sympy import sympify, N as sympy_N, parse_expr, sqrt  # noqa: F401

        # Строим пространство имён
        ns = {"sqrt": sqrt, "abs": abs}
        for k, v in original_values.items():
            ns[k] = sympify(v)

        expr = parse_expr(formula_text, local_dict=ns)
        result = float(
            sympy_N(
                expr.subs({sympify(k): sympify(v) for k, v in original_values.items()})
            )
        )

        if math.isnan(result) or math.isinf(result):
            return False

        # Допуск 0.5% или абсолютный 0.01
        tol = max(abs(expected_answer) * 0.005, 0.01)
        return abs(result - expected_answer) < tol
    except Exception as exc:
        log.debug("Verification failed: %s", exc)
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Parse Gemini response -> TaskTemplate
# ═══════════════════════════════════════════════════════════════════════════


def _parse_response(data: dict) -> Optional[TaskTemplate]:
    """Преобразует JSON-ответ Gemini в TaskTemplate."""
    try:
        task_type = data.get("task_type", "arithmetic")
        text_template = data.get("text_template", "")
        answer_formula = data.get("answer_formula", "")
        constraints = data.get("constraints", [])
        raw_params = data.get("params", {})

        if not text_template or not answer_formula or not raw_params:
            return None

        params: Dict[str, ParamInfo] = {}
        original_values: Dict[str, Any] = {}

        for pname, pinfo in raw_params.items():
            orig = pinfo.get("original_value", 0)
            vtype = pinfo.get("type", "int")
            params[pname] = ParamInfo(
                name=pname,
                original_value=int(orig)
                if vtype == "int" and orig == int(orig)
                else orig,
                min_val=int(pinfo.get("min", -50)),
                max_val=int(pinfo.get("max", 50)),
                value_type=vtype,
                exclude=pinfo.get("exclude", []),
            )
            original_values[pname] = params[pname].original_value

        return TaskTemplate(
            task_type=task_type,
            text_template=text_template,
            params=params,
            original_values=original_values,
            answer_formula_repr=answer_formula,  # LLM даёт текстовую формулу
            answer_formula_text=answer_formula,
            constraints_repr=constraints,
            quality_score=0.0,  # будет обновлено после верификации
            sympy_expr_repr="",
        )
    except Exception as exc:
        log.warning("Failed to parse LLM response: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════


def templatize_via_llm(
    task_text: str,
    latex: str = "",
    correct_answer: str = "",
    answer_type: str = "exact_number",
    *,
    max_attempts: int = 2,
) -> Optional[TaskTemplate]:
    """
    Шаблонизирует задачу через Gemini Flash.

    Args:
        task_text: текст задачи
        latex: LaTeX условия (необязательно)
        correct_answer: правильный ответ (строка)
        answer_type: тип ответа (exact_number, decimal, fraction)
        max_attempts: макс. попыток (1-я обычная, 2-я с правкой)

    Returns:
        TaskTemplate если успешно, None если невозможно шаблонизировать.
    """
    from src.domain.gemini_client import (
        call_gemini,
        get_model,
        parse_json_response,
    )

    if not task_text or not correct_answer:
        return None

    # Парсим числовой ответ
    try:
        expected = float(correct_answer.replace(",", ".").strip())
    except ValueError:
        log.debug("Cannot parse answer '%s' as number, skipping", correct_answer)
        return None

    prompt = _build_prompt(task_text, latex, correct_answer, answer_type)

    for attempt in range(max_attempts):
        try:
            raw = call_gemini(
                prompt,
                model=get_model("flash"),
                temperature=0.1 if attempt == 0 else 0.3,
                json_mode=True,
                max_tokens=2048,
                max_retries=2,
            )
            data = parse_json_response(raw)
            if not isinstance(data, dict):
                log.warning("LLM returned non-dict: %s", type(data))
                continue

            tpl = _parse_response(data)
            if tpl is None:
                log.debug("Attempt %d: parsing failed", attempt + 1)
                continue

            # ── Верификация через SymPy ──
            if _verify_formula(tpl.answer_formula_text, tpl.original_values, expected):
                tpl.quality_score = 0.95
                log.info(
                    "LLM templatize OK (attempt %d): type=%s, params=%s",
                    attempt + 1,
                    tpl.task_type,
                    list(tpl.params.keys()),
                )
                return tpl

            log.debug(
                "Attempt %d: formula verification failed for '%s'",
                attempt + 1,
                tpl.answer_formula_text,
            )
            # Добавляем подсказку для следующей попытки
            prompt += (
                f"\n\n⚠️ Попытка {attempt + 1} не прошла верификацию. "
                f"Формула «{tpl.answer_formula_text}» с параметрами "
                f"{tpl.original_values} даёт неправильный результат. "
                f"Ожидаемый ответ: {correct_answer}. Исправь формулу."
            )

        except Exception as exc:
            log.warning("LLM call failed (attempt %d): %s", attempt + 1, exc)

    log.info(
        "LLM templatize FAILED after %d attempts for: %s", max_attempts, task_text[:80]
    )
    return None
