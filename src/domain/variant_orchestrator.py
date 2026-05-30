"""
ALGO V1 — Variant Orchestrator
src/exam_builder/variant_orchestrator.py

Координатор генерации вариантов экзамена.

Полный цикл (вызывается ПОСЛЕ approve черновика):
  1. Берёт задачи из основного варианта (draft)
  2. Для каждой числовой задачи: LLM templatize (Gemini Flash)
  3. Генерирует N уникальных наборов параметров
  4. StepSolver решает по шагам
  5. StepDistractorEngine создаёт дистракторы
  6. Формирует итоговые варианты (JSONB-совместимые)

Ограничение: до 30 вариантов.
Fallback: если задачу нельзя шаблонизировать — остаётся оригинальной.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4

from src.domain.template_models import TaskTemplate
from src.domain.llm_templatizer import (
    templatize_via_llm,
    TEMPLATABLE_TYPES,
)
from src.domain.sympy_step_solver import (
    StepSolver,
    StepDistractorEngine,
)
from src.domain.template_cache import TemplateCache

log = logging.getLogger("variant_orchestrator")

MAX_VARIANTS = 30
MAX_ATTEMPTS_PER_VARIANT = 200  # попыток подбора уникального набора параметров


# ═══════════════════════════════════════════════════════════════════════════
# Data models
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class VariantTask:
    """Одна задача в варианте (сериализуется в JSONB)."""

    task_id: str
    question_text: str
    correct_answer: str
    answer_type: str = "exact_number"

    skill_id: str = ""
    skill_name: str = ""
    difficulty: str = "medium"

    # Дистракторы
    answer_options: List[str] = field(default_factory=list)
    distractor_meta: List[Dict[str, Any]] = field(default_factory=list)

    # Шаги решения
    solution_steps: List[Dict[str, str]] = field(default_factory=list)

    # Источник
    source_task_id: str = ""  # ID оригинальной задачи
    is_original: bool = True  # False если сгенерирован из шаблона
    param_values: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Сериализация для JSONB."""
        return {
            "task_id": self.task_id,
            "question_text": self.question_text,
            "correct_answer": self.correct_answer,
            "answer_type": self.answer_type,
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "difficulty": self.difficulty,
            "answer_options": self.answer_options,
            "distractor_meta": self.distractor_meta,
            "solution_steps": self.solution_steps,
            "source_task_id": self.source_task_id,
            "is_original": self.is_original,
            "param_values": self.param_values,
        }


@dataclass
class GeneratedVariant:
    """Вариант экзамена (сериализуется в JSONB)."""

    variant_number: int
    tasks: List[VariantTask]
    seed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "variant_number": self.variant_number,
            "tasks": [t.to_dict() for t in self.tasks],
            "seed": self.seed,
        }


@dataclass
class GenerationReport:
    """Отчёт о генерации вариантов."""

    total_variants: int
    tasks_templatized: int  # задач успешно шаблонизированных
    tasks_fallback: int  # задач оставленных оригинальными
    tasks_total: int
    variants: List[GeneratedVariant] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# Main Orchestrator
# ═══════════════════════════════════════════════════════════════════════════


class VariantOrchestrator:
    """
    Координирует полный цикл генерации вариантов.

    Usage:
        orch = VariantOrchestrator()
        report = orch.generate_variants(draft_tasks, num_variants=10)
        # report.variants — список GeneratedVariant

    Если передан ``db`` — сначала ищет шаблон в ``task_templates``.
    Fallback: on-the-fly шаблонизация через SymPy.
    """

    def __init__(self, db=None, *, persist_new: bool = True):
        """
        Args:
            db: SQLAlchemy Session (optional). Если передан —
                шаблоны сначала ищутся в БД (task_templates).
            persist_new: если True и db передан — on-the-fly шаблоны
                         автоматически сохраняются в БД для будущих запросов.
        """
        self.step_solver = StepSolver()
        self.distractor_engine = StepDistractorEngine()
        self.db = db
        self.persist_new = persist_new

    def generate_variants(
        self,
        draft_tasks: List[Dict[str, Any]],
        num_variants: int = 2,
        seed: int = 42,
    ) -> GenerationReport:
        """
        Генерирует варианты из черновика.

        Args:
            draft_tasks: список задач из JSONB draft варианта.
                         Каждая задача: {task_id, question_text, correct_answer,
                                        answer_type, skill_id, difficulty, ...}
            num_variants: количество вариантов (1–30)
            seed: для воспроизводимости

        Returns:
            GenerationReport
        """
        num_variants = min(num_variants, MAX_VARIANTS)
        rng = random.Random(seed)

        # Pre-load cached templates in one batch query
        _cached_map: Dict[str, TaskTemplate] = {}
        if self.db is not None:
            task_ids = [t.get("task_id", "") for t in draft_tasks if t.get("task_id")]
            if task_ids:
                try:
                    _cached_map = TemplateCache.get_cached_bulk(task_ids, self.db)
                    log.info(
                        "Bulk cache: %d/%d templates found in DB",
                        len(_cached_map),
                        len(task_ids),
                    )
                except Exception as exc:
                    log.warning("Bulk cache preload failed: %s", exc)
                    if self.db is not None:
                        self.db.rollback()

        # Phase 1: шаблонизация всех задач
        templates: List[Optional[TaskTemplate]] = []
        tasks_ok = 0
        tasks_fb = 0

        for task in draft_tasks:
            tpl = self._templatize_task(task)
            templates.append(tpl)
            if tpl is not None:
                tasks_ok += 1
            else:
                tasks_fb += 1

        log.info(
            "Templatized %d/%d tasks (%d fallback)",
            tasks_ok,
            len(draft_tasks),
            tasks_fb,
        )

        # Phase 2: генерация уникальных наборов параметров для каждого шаблона
        #   param_sets[i] = [set1, set2, ...] — по одному набору на вариант
        param_sets: List[List[Optional[Dict[str, Any]]]] = []
        for i, tpl in enumerate(templates):
            if tpl is None:
                param_sets.append([None] * num_variants)
            else:
                sets = self._generate_unique_param_sets(tpl, num_variants, rng)
                param_sets.append(sets)

        # Phase 3: собираем варианты
        variants: List[GeneratedVariant] = []
        errors: List[str] = []

        for v_idx in range(num_variants):
            variant_seed = seed + v_idx * 1000
            variant_tasks: List[VariantTask] = []

            for t_idx, task in enumerate(draft_tasks):
                tpl = templates[t_idx]
                pset = param_sets[t_idx][v_idx] if t_idx < len(param_sets) else None

                if tpl is not None and pset is not None:
                    # Генерируем задачу из шаблона
                    try:
                        vt = self._build_variant_task(task, tpl, pset, v_idx)
                        variant_tasks.append(vt)
                    except Exception as exc:
                        errors.append(f"Variant {v_idx + 1}, task {t_idx + 1}: {exc}")
                        variant_tasks.append(self._make_original_task(task))
                else:
                    # Fallback: оригинальная задача
                    variant_tasks.append(self._make_original_task(task))

            variants.append(
                GeneratedVariant(
                    variant_number=v_idx + 1,
                    tasks=variant_tasks,
                    seed=variant_seed,
                )
            )

        return GenerationReport(
            total_variants=num_variants,
            tasks_templatized=tasks_ok,
            tasks_fallback=tasks_fb,
            tasks_total=len(draft_tasks),
            variants=variants,
            errors=errors,
        )

    # ─────────────────────────────────────────────────────────
    # Phase 1: Templatize
    # ─────────────────────────────────────────────────────────

    def _templatize_task(self, task: Dict[str, Any]) -> Optional[TaskTemplate]:
        """
        Шаблонизирует одну задачу draft'а.

        Порядок:
          1. Проверить кэш в БД (task_templates) — если db передан
          2. Fallback: on-the-fly через SymPyTemplateEngine
          3. Если получилось on-the-fly и persist_new — сохранить в БД
        """
        task_id = task.get("task_id", "")
        text = task.get("question_text", task.get("text", ""))
        latex = task.get("question_latex", task.get("latex", ""))
        answer = task.get("correct_answer", task.get("answer", ""))
        answer_type = task.get("answer_type", "exact_number")

        if not text or not answer:
            return None

        # ── 0. Фильтр: только числовые типы ───
        if answer_type not in TEMPLATABLE_TYPES:
            log.debug(
                "Skipping non-numeric task %s (answer_type=%s)", task_id, answer_type
            )
            return None

        # ── 1. Cache hit ──
        if self.db is not None and task_id:
            try:
                cached = TemplateCache.get_cached(task_id, self.db)
                if cached is not None:
                    log.debug(
                        "Cache HIT for task %s (q=%.2f)", task_id, cached.quality_score
                    )
                    return cached
            except Exception as exc:
                log.debug("Cache lookup failed for %s: %s", task_id, exc)

        # ── 2. On-the-fly: LLM templatize ──
        try:
            tpl = templatize_via_llm(text, latex, answer, answer_type)

            # ── 3. Persist new template ──
            if tpl is not None and self.db is not None and self.persist_new and task_id:
                try:
                    tpl_id = f"TPL_{task_id}"
                    skill_id = task.get("skill_id", "")
                    TemplateCache.save_template(tpl_id, task_id, skill_id, tpl, self.db)
                    log.debug("Persisted new template %s for task %s", tpl_id, task_id)
                except Exception as exc:
                    log.debug("Failed to persist template for %s: %s", task_id, exc)

            return tpl
        except Exception as exc:
            log.debug("Templatize failed for task %s: %s", task_id, exc)
            return None

    # ─────────────────────────────────────────────────────────
    # Phase 2: Unique parameter sets
    # ─────────────────────────────────────────────────────────

    def _generate_unique_param_sets(
        self,
        tpl: TaskTemplate,
        count: int,
        rng: random.Random,
    ) -> List[Optional[Dict[str, Any]]]:
        """
        Генерирует N уникальных наборов параметров для шаблона.

        Гарантии:
          - Все наборы различны (по frozenset)
          - Все удовлетворяют constraints
          - Ответ — «красивое» число (целое если исходный целый)
          - Первый набор = оригинальные значения
        """
        from sympy import sympify, N as sympy_N, parse_expr

        results: List[Optional[Dict[str, Any]]] = []
        seen: Set[frozenset] = set()

        answer_expr = parse_expr(tpl.answer_formula_text)

        original_is_int = all(isinstance(v, int) for v in tpl.original_values.values())

        # Набор 0 = оригинальные значения
        results.append(dict(tpl.original_values))
        seen.add(frozenset(tpl.original_values.items()))

        # Остальные — случайные
        attempts = 0
        while len(results) < count and attempts < MAX_ATTEMPTS_PER_VARIANT * count:
            attempts += 1
            values: Dict[str, Any] = {}

            for pname, pinfo in tpl.params.items():
                if pinfo.value_type == "int":
                    values[pname] = rng.randint(pinfo.min_val, pinfo.max_val)
                else:
                    values[pname] = round(rng.uniform(pinfo.min_val, pinfo.max_val), 2)

            # Проверяем excludes
            skip = False
            for pname, pinfo in tpl.params.items():
                if values[pname] in pinfo.exclude:
                    skip = True
                    break
            if skip:
                continue

            # Проверяем уникальность
            key = frozenset(values.items())
            if key in seen:
                continue

            # Проверяем constraints
            if not self._check_constraints(tpl.constraints_repr, values):
                continue

            # Вычисляем ответ и проверяем «красивость»
            try:
                syms = {k: sympify(v) for k, v in values.items()}
                result = float(sympy_N(answer_expr.subs(syms)))
                if math.isnan(result) or math.isinf(result):
                    continue
                # Если оригинал целый — ответ тоже должен быть целым
                if original_is_int and abs(result - round(result)) > 0.001:
                    continue
                # Ответ не должен быть слишком большим
                if abs(result) > 10000:
                    continue
            except Exception:
                continue

            seen.add(key)
            results.append(values)

        # Добиваем None если не хватило
        while len(results) < count:
            results.append(None)

        return results

    @staticmethod
    def _check_constraints(
        constraints: List[str],
        values: Dict[str, Any],
    ) -> bool:
        """Проверяет constraints для набора значений."""
        from sympy import sympify, parse_expr

        try:
            syms = {k: sympify(v) for k, v in values.items()}
            for cstr in constraints:
                if not cstr or cstr == "True":
                    continue
                try:
                    expr = parse_expr(cstr)
                    result = expr.subs(syms)
                    if result == False:  # noqa: E712
                        return False
                except Exception:
                    # Если constraint не парсится — пропускаем
                    continue
            return True
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────
    # Phase 3: Build variant task
    # ─────────────────────────────────────────────────────────

    def _build_variant_task(
        self,
        original_task: Dict[str, Any],
        tpl: TaskTemplate,
        param_values: Dict[str, Any],
        variant_idx: int,
    ) -> VariantTask:
        """Строит одну задачу варианта из шаблона + значений."""
        from sympy import sympify, N as sympy_N, parse_expr

        # 1. Генерируем текст
        try:
            question_text = tpl.text_template.format(**param_values)
        except Exception:
            question_text = tpl.text_template
            for k, v in param_values.items():
                question_text = question_text.replace("{" + k + "}", str(v))

        # 2. Вычисляем ответ
        answer_expr = parse_expr(tpl.answer_formula_text)
        syms = {k: sympify(v) for k, v in param_values.items()}
        result = float(sympy_N(answer_expr.subs(syms)))

        if result == int(result):
            answer_str = str(int(result))
        else:
            answer_str = f"{result:.4g}"

        # 3. Решаем по шагам
        steps = self.step_solver.solve(
            tpl.task_type,
            tpl.answer_formula_text,
            param_values,
            tpl.text_template,
        )
        steps_dicts = [
            {
                "step": s.step_number,
                "description": s.description,
                "expression": s.expression,
                "result": s.result,
            }
            for s in steps
        ]

        # 4. Генерируем дистракторы
        distractors = self.distractor_engine.generate(
            steps,
            result,
            tpl.task_type,
            count=3,
        )
        options = [answer_str] + [d.value for d in distractors]
        # Перемешиваем
        random.shuffle(options)

        distractor_meta = [
            {
                "value": d.value,
                "error_type": d.error_type,
                "broken_step": d.broken_step,
                "plausibility": d.plausibility,
                "explanation": d.explanation,
            }
            for d in distractors
        ]

        return VariantTask(
            task_id=f"VT_{uuid4().hex[:8]}",
            question_text=question_text,
            correct_answer=answer_str,
            answer_type=original_task.get("answer_type", "exact_number"),
            skill_id=original_task.get("skill_id", ""),
            skill_name=original_task.get("skill_name", ""),
            difficulty=original_task.get("difficulty", "medium"),
            answer_options=options,
            distractor_meta=distractor_meta,
            solution_steps=steps_dicts,
            source_task_id=original_task.get("task_id", ""),
            is_original=False,
            param_values=param_values,
        )

    @staticmethod
    def _make_original_task(task: Dict[str, Any]) -> VariantTask:
        """Создаёт VariantTask-обёртку для оригинальной задачи (fallback)."""
        return VariantTask(
            task_id=task.get("task_id", f"VT_{uuid4().hex[:8]}"),
            question_text=task.get("question_text", task.get("text", "")),
            correct_answer=task.get("correct_answer", task.get("answer", "")),
            answer_type=task.get("answer_type", "exact_number"),
            skill_id=task.get("skill_id", ""),
            skill_name=task.get("skill_name", ""),
            difficulty=task.get("difficulty", "medium"),
            answer_options=task.get("answer_options", task.get("options", [])) or [],
            distractor_meta=task.get("distractor_meta", []),
            solution_steps=[],
            source_task_id=task.get("task_id", ""),
            is_original=True,
            param_values={},
        )
