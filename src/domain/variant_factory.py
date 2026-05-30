"""
ALGO V1 - Variant Factory
Генерация вариантов экзамена с шаблонной параметризацией.

Берёт задачу из tasks_master → автопараметризация (числа → шаблон) →
подстановка новых чисел → SymPy/eval решение → DistractorGen для нового ответа.

Для задач без шаблона — fallback: берёт другую задачу того же skill/difficulty.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from src.domain.safe_eval import safe_eval


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TemplateParams:
    """Параметры шаблона задачи"""

    template_text: str  # "Решите: {a}x + {b} = {c}"
    template_latex: Optional[str]
    parameters: List[str]  # ['a', 'b', 'c']
    constraints: Dict[
        str, Dict[str, Any]
    ]  # {'a': {'min': -10, 'max': 10, 'type': 'int', 'exclude': [0]}}
    answer_formula: str  # "(c - b) / a"
    original_values: Dict[str, int]  # {'a': 3, 'b': -7, 'c': 14}


@dataclass
class GeneratedVariantTask:
    """Задача, сгенерированная для нового варианта"""

    task_id: str  # original or "GEN_xxx"
    original_task_id: str  # ID оригинала в tasks_master
    skill_id: str
    skill_name: str
    difficulty: str
    question_text: str
    question_latex: Optional[str]
    question_image_url: Optional[str]
    answer_type: str
    correct_answer: str
    answer_options: Optional[List[str]]
    distractor_meta: Optional[List[Dict]]
    is_template_generated: bool  # True если числа изменены


# ---------------------------------------------------------------------------
# VariantFactory
# ---------------------------------------------------------------------------


class VariantFactory:
    """
    Генерирует варианты экзамена.

    Для каждой задачи:
    1. Пытается параметризовать (найти числа, создать шаблон)
    2. Генерирует новые числа с учётом constraints
    3. Вычисляет новый ответ через формулу
    4. Генерирует дистракторы для нового ответа
    """

    def generate_variant_tasks(
        self,
        original_tasks: List[Dict[str, Any]],
        variant_seed: int = 0,
    ) -> List[GeneratedVariantTask]:
        """
        Генерирует набор задач для нового варианта.
        original_tasks: список dict из variants JSONB (с correct_answer, question_text, etc.)
        """
        rng = random.Random(variant_seed)
        result = []

        for task in original_tasks:
            gen = self._generate_single(task, rng)
            result.append(gen)

        return result

    def _generate_single(
        self, task: Dict[str, Any], rng: random.Random
    ) -> GeneratedVariantTask:
        """Генерирует один вариант задачи"""
        original_text = task.get("question_text", "")
        original_latex = task.get("question_latex")
        correct = task.get("correct_answer", "")

        # Пытаемся параметризовать
        template = self._parameterize(original_text, original_latex, correct)

        if template and template.answer_formula:
            # Генерируем новые значения
            new_values = self._generate_values(template, rng)
            if new_values:
                new_text = self._substitute(template.template_text, new_values)
                new_latex = (
                    self._substitute(template.template_latex, new_values)
                    if template.template_latex
                    else None
                )
                new_answer = self._compute_answer(template.answer_formula, new_values)

                if new_answer is not None:
                    new_answer_str = self._format_number(new_answer)

                    # Генерируем дистракторы для нового ответа
                    distractors, distractor_meta = self._generate_distractors(
                        new_answer_str, new_text, new_latex
                    )

                    return GeneratedVariantTask(
                        task_id=f"GEN_{uuid4().hex[:8]}",
                        original_task_id=task.get("task_id", ""),
                        skill_id=task.get("skill_id", ""),
                        skill_name=task.get("skill_name", ""),
                        difficulty=task.get("difficulty", "B"),
                        question_text=new_text,
                        question_latex=new_latex,
                        question_image_url=None,  # Images don't change
                        answer_type=task.get("answer_type", "exact_number"),
                        correct_answer=new_answer_str,
                        answer_options=distractors,
                        distractor_meta=distractor_meta,
                        is_template_generated=True,
                    )

        # Fallback: вернуть оригинал без изменений (с пометкой)
        distractors, distractor_meta = self._generate_distractors(
            correct, original_text, original_latex
        )
        return GeneratedVariantTask(
            task_id=task.get("task_id", ""),
            original_task_id=task.get("task_id", ""),
            skill_id=task.get("skill_id", ""),
            skill_name=task.get("skill_name", ""),
            difficulty=task.get("difficulty", "B"),
            question_text=original_text,
            question_latex=original_latex,
            question_image_url=task.get("question_image_url"),
            answer_type=task.get("answer_type", "exact_number"),
            correct_answer=correct,
            answer_options=task.get("answer_options") or distractors,
            distractor_meta=distractor_meta,
            is_template_generated=False,
        )

    # ────────────────── Параметризация ──────────────────

    def _parameterize(
        self, text: str, latex: Optional[str], correct_answer: str
    ) -> Optional[TemplateParams]:
        """Автоматически параметризует задачу через LLM Templatizer."""
        if not text:
            return None

        try:
            from src.domain.llm_templatizer import templatize_via_llm

            tpl = templatize_via_llm(text, latex or "", correct_answer, "exact_number")
            if tpl is None:
                return None

            # Конвертируем TaskTemplate → TemplateParams
            constraints = {}
            for pname, pinfo in tpl.params.items():
                constraints[pname] = {
                    "min": pinfo.min_val,
                    "max": pinfo.max_val,
                    "type": pinfo.value_type,
                    "exclude": list(pinfo.exclude),
                }

            return TemplateParams(
                template_text=tpl.text_template,
                template_latex=tpl.latex_template,
                parameters=list(tpl.params.keys()),
                constraints=constraints,
                answer_formula=tpl.answer_formula_text,
                original_values=dict(tpl.original_values),
            )
        except Exception:
            return None

    # ────────────────── Подстановка ──────────────────

    def _generate_values(
        self, template: TemplateParams, rng: random.Random, max_attempts: int = 100
    ) -> Optional[Dict[str, Any]]:
        """Генерирует новые случайные значения для параметров"""
        for _ in range(max_attempts):
            values = {}
            valid = True

            for param in template.parameters:
                c = template.constraints.get(param, {})
                min_v = c.get("min", -10)
                max_v = c.get("max", 10)
                exclude = set(c.get("exclude", []))
                param_type = c.get("type", "int")

                if param_type == "int":
                    val = rng.randint(min_v, max_v)
                    attempts = 0
                    while val in exclude and attempts < 50:
                        val = rng.randint(min_v, max_v)
                        attempts += 1
                    if val in exclude:
                        valid = False
                        break
                else:
                    val = round(rng.uniform(min_v, max_v), 2)

                values[param] = val

            if not valid:
                continue

            # Проверяем что ответ вычислим и "красивый"
            if template.answer_formula:
                answer = self._compute_answer(template.answer_formula, values)
                if answer is None:
                    continue
                # Для целочисленных задач — ответ должен быть целым
                if all(isinstance(v, int) for v in values.values()):
                    if answer != int(answer):
                        continue

            # Убедимся что значения отличаются от оригинала
            if values == template.original_values:
                continue

            return values

        return None

    def _substitute(self, template: Optional[str], values: Dict[str, Any]) -> str:
        """Подставляет значения в шаблон"""
        if not template:
            return ""
        result = template
        for param, val in values.items():
            placeholder = "{" + param + "}"
            if isinstance(val, int):
                result = result.replace(placeholder, str(val))
            else:
                result = result.replace(placeholder, f"{val:.2g}")
        return result

    def _compute_answer(self, formula: str, values: Dict[str, Any]) -> Optional[float]:
        """Вычисляет ответ по формуле (SymPy + safe_eval fallback)."""
        if not formula:
            return None
        try:
            from sympy import sympify, N as sympy_N, parse_expr

            answer_expr = parse_expr(formula)
            syms = {k: sympify(v) for k, v in values.items()}
            result = float(sympy_N(answer_expr.subs(syms)))
            if not math.isnan(result) and not math.isinf(result):
                return result
        except Exception:
            pass
        # Fallback на safe_eval
        try:
            result = safe_eval(formula, values)
            if (
                isinstance(result, (int, float))
                and not math.isnan(result)
                and not math.isinf(result)
            ):
                return float(result)
        except Exception:
            pass
        return None

    @staticmethod
    def _format_number(val: float) -> str:
        """Форматирует число без лишних нулей"""
        if val == int(val):
            return str(int(val))
        return f"{val:.4g}"

    # ────────────────── Дистракторы ──────────────────

    def _generate_distractors(
        self, answer: str, question_text: str, question_latex: Optional[str]
    ) -> Tuple[Optional[List[str]], Optional[List[Dict]]]:
        """Генерирует дистракторы: StepDistractorEngine → DistractorGen → fallback."""
        # 1) Пробуем StepDistractorEngine через LLM templatize
        try:
            from src.domain.sympy_step_solver import StepDistractorEngine
            from src.domain.llm_templatizer import templatize_via_llm

            tpl = templatize_via_llm(
                question_text, question_latex or "", answer, "exact_number"
            )
            if tpl is not None:
                from src.domain.sympy_step_solver import StepSolver

                solver = StepSolver()
                steps = solver.solve(
                    tpl.task_type,
                    tpl.answer_formula_text,
                    dict(tpl.original_values),
                    tpl.text_template,
                )
                if steps:
                    dist_engine = StepDistractorEngine()
                    ans_float = float(answer.replace(",", "."))
                    distractors = dist_engine.generate(
                        steps, ans_float, tpl.task_type, count=3
                    )
                    if distractors:
                        opts = [answer] + [d.value for d in distractors]
                        random.shuffle(opts)
                        meta = [
                            {
                                "value": d.value,
                                "error_type": d.error_type,
                                "broken_step": d.broken_step,
                                "plausibility": d.plausibility,
                                "explanation": d.explanation,
                            }
                            for d in distractors
                        ]
                        return opts, meta
        except Exception:
            pass

        # 2) Fallback: generate_for_answer (все типы: числовые, дроби, выражения)
        try:
            from src.domain.sympy_step_solver import StepDistractorEngine

            dist_engine = StepDistractorEngine()
            distractors = dist_engine.generate_for_answer(
                correct_answer=answer,
                answer_type="exact_number",
                count=3,
            )
            if distractors:
                opts = [answer] + [d.value for d in distractors]
                random.shuffle(opts)
                meta = [
                    {
                        "value": d.value,
                        "error_type": d.error_type,
                        "broken_step": d.broken_step,
                        "plausibility": d.plausibility,
                        "explanation": d.explanation,
                    }
                    for d in distractors
                ]
                return opts, meta
        except Exception:
            pass

        # 3) Arithmetic fallback
        try:
            val = float(answer.replace(",", "."))
            opts = [
                answer,
                self._format_number(val + 1),
                self._format_number(val - 1),
                self._format_number(-val) if val != 0 else self._format_number(val + 2),
            ]
            random.shuffle(opts)
            return opts, [
                {
                    "value": self._format_number(val + 1),
                    "error_type": "off_by_one",
                    "explanation": "Ошибка на +1",
                    "plausibility": 0.65,
                },
                {
                    "value": self._format_number(val - 1),
                    "error_type": "off_by_one",
                    "explanation": "Ошибка на −1",
                    "plausibility": 0.65,
                },
                {
                    "value": self._format_number(-val),
                    "error_type": "sign_error",
                    "explanation": "Ошибка знака",
                    "plausibility": 0.80,
                },
            ]
        except ValueError:
            return None, None


def generate_variant_json(
    original_tasks: List[Dict[str, Any]],
    variant_number: int,
) -> Dict[str, Any]:
    """
    Удобная обёртка: берёт список задач из варианта 1,
    возвращает готовый вариант JSONB dict.
    """
    factory = VariantFactory()
    gen_tasks = factory.generate_variant_tasks(
        original_tasks, variant_seed=variant_number * 42
    )

    tasks_json = []
    for t in gen_tasks:
        tasks_json.append(
            {
                "task_id": t.task_id,
                "original_task_id": t.original_task_id,
                "skill_id": t.skill_id,
                "skill_name": t.skill_name,
                "difficulty": t.difficulty,
                "question_text": t.question_text,
                "question_latex": t.question_latex,
                "question_image_url": t.question_image_url,
                "answer_type": t.answer_type,
                "correct_answer": t.correct_answer,
                "answer_options": t.answer_options,
                "distractor_meta": t.distractor_meta,
                "is_template_generated": t.is_template_generated,
            }
        )

    return {
        "variant_id": f"VAR_{uuid4().hex[:8]}",
        "variant_number": variant_number,
        "tasks": tasks_json,
        "total_points": len(tasks_json) * 10,
    }
