"""
ALGO V1 — Template Cache
src/exam_builder/template_cache.py

Читает / записывает параметрические шаблоны из таблицы ``task_templates``.
Является мостом между хранилищем (PostgreSQL) и in-memory ``TaskTemplate``
из ``template_models``.

Поток:
  1. При оцифровке задачи (AI / методист) — шаблон записывается в БД
     через ``save_template()`` или admin-endpoint.
  2. При генерации вариантов ``VariantOrchestrator`` вызывает
     ``get_cached()`` — если есть в БД, возвращает ``TaskTemplate``,
     иначе ``None`` (и оркестратор fallback'ит на on-the-fly LLM).
  3. После on-the-fly шаблонизации оркестратор может сохранить
     результат через ``save_template()`` для будущих запросов.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from src.domain.template_models import ParamInfo, TaskTemplate
from src.models import TaskTemplateDB

log = logging.getLogger("template_cache")


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════


class TemplateCache:
    """
    Кэш шаблонов — мост между PostgreSQL ``task_templates`` и
    in-memory ``TaskTemplate`` dataclass.
    """

    # ── read ────────────────────────────────────────────────

    @staticmethod
    def get_cached(
        task_id: str,
        db: Session,
    ) -> Optional[TaskTemplate]:
        """
        Достаёт кэшированный шаблон по ``original_task_id``.

        Returns:
            ``TaskTemplate`` если найден активный шаблон, иначе ``None``.
        """
        row: Optional[TaskTemplateDB] = (
            db.query(TaskTemplateDB)
            .filter(
                TaskTemplateDB.original_task_id == task_id,
                TaskTemplateDB.is_active.is_(True),
            )
            .order_by(TaskTemplateDB.quality_score.desc())
            .first()
        )
        if row is None:
            return None

        try:
            return _row_to_template(row)
        except Exception as exc:  # pragma: no cover — защита от битых данных
            log.warning("Cannot convert DB row %s → TaskTemplate: %s", row.id, exc)
            return None

    @staticmethod
    def get_cached_bulk(
        task_ids: List[str],
        db: Session,
    ) -> Dict[str, TaskTemplate]:
        """
        Пакетная выборка шаблонов.

        Returns:
            ``{original_task_id: TaskTemplate}`` для найденных.
        """
        if not task_ids:
            return {}

        rows = (
            db.query(TaskTemplateDB)
            .filter(
                TaskTemplateDB.original_task_id.in_(task_ids),
                TaskTemplateDB.is_active.is_(True),
            )
            .order_by(TaskTemplateDB.quality_score.desc())
            .all()
        )

        result: Dict[str, TaskTemplate] = {}
        for row in rows:
            # Первый (наивысший quality_score) — побеждает
            if row.original_task_id in result:
                continue
            try:
                result[row.original_task_id] = _row_to_template(row)
            except Exception as exc:  # pragma: no cover
                log.warning("Skipping broken template %s: %s", row.id, exc)
        return result

    # ── write ───────────────────────────────────────────────

    @staticmethod
    def save_template(
        template_id: str,
        task_id: str,
        skill_id: str,
        tpl: TaskTemplate,
        db: Session,
        *,
        generation_method: str = "sympy_verified",
    ) -> str:
        """
        Сохраняет / обновляет шаблон в БД.

        Returns:
            ID записи.
        """
        existing: Optional[TaskTemplateDB] = (
            db.query(TaskTemplateDB).filter(TaskTemplateDB.id == template_id).first()
        )

        param_details = {
            name: {
                "min": p.min_val,
                "max": p.max_val,
                "type": p.value_type,
                "exclude": p.exclude,
            }
            for name, p in tpl.params.items()
        }

        values = dict(
            original_task_id=task_id,
            skill_id=skill_id,
            template_text=_to_double_braces(tpl.text_template),
            text_template=tpl.text_template,
            parameters=list(tpl.params.keys()),
            constraints=_param_details_to_constraints(param_details),
            answer_formula=tpl.answer_formula_text,
            answer_formula_sympy=tpl.answer_formula_repr,
            constraints_repr=tpl.constraints_repr,
            task_type=tpl.task_type,
            original_values=tpl.original_values,
            param_details=param_details,
            generation_method=generation_method,
            quality_score=tpl.quality_score,
            is_active=True,
        )

        if existing:
            for k, v in values.items():
                setattr(existing, k, v)
            db.flush()
            return existing.id

        row = TaskTemplateDB(id=template_id, **values)
        db.add(row)
        db.flush()
        return row.id


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════


def _row_to_template(row: TaskTemplateDB) -> TaskTemplate:
    """
    Конвертирует DB-строку → in-memory ``TaskTemplate``.

    Приоритет полей:
        - ``text_template``  (формат {a}) → используется если есть
        - ``template_text``  (формат {{a}}) → конвертируется
    """
    # ── text_template ──
    text_tpl: str = row.text_template or ""
    if not text_tpl and row.template_text:
        # Конвертируем {{a}} → {a}
        text_tpl = row.template_text.replace("{{", "{").replace("}}", "}")
    if not text_tpl:
        raise ValueError(f"Template {row.id} has no text_template or template_text")

    # ── params ──
    params = _build_params(row)

    # ── original_values ──
    original_values: Dict = row.original_values or {}

    # ── answer formula ──
    answer_formula_repr = row.answer_formula_sympy or ""
    answer_formula_text = row.answer_formula or ""

    # ── constraints ──
    constraints_repr: list = row.constraints_repr or []

    return TaskTemplate(
        task_type=row.task_type or "arithmetic",
        text_template=text_tpl,
        params=params,
        original_values=original_values,
        answer_formula_repr=answer_formula_repr,
        answer_formula_text=answer_formula_text,
        constraints_repr=constraints_repr,
        quality_score=float(row.quality_score or 0),
    )


def _build_params(row: TaskTemplateDB) -> Dict[str, ParamInfo]:
    """Собирает ``Dict[str, ParamInfo]`` из ``param_details`` или ``constraints``."""
    details: Dict = row.param_details or {}
    param_names: list = row.parameters or []

    if details:
        # Формат из 012: {"a": {"min": 1, "max": 50, "type": "int", "exclude": [0]}}
        result = {}
        for name, info in details.items():
            result[name] = ParamInfo(
                name=name,
                original_value=(row.original_values or {}).get(name, 0),
                min_val=int(info.get("min", -50)),
                max_val=int(info.get("max", 50)),
                value_type=info.get("type", "int"),
                exclude=info.get("exclude", []),
            )
        return result

    # Fallback — legacy формат из 001: constraints = {"a": {"min":1, "max":10, "type":"int"}}
    legacy: Dict = row.constraints or {}
    result = {}
    for name in param_names:
        info = legacy.get(name, {})
        if isinstance(info, dict):
            result[name] = ParamInfo(
                name=name,
                original_value=(row.original_values or {}).get(name, 0),
                min_val=int(info.get("min", -50)),
                max_val=int(info.get("max", 50)),
                value_type=info.get("type", "int"),
                exclude=info.get("exclude", []),
            )
        else:
            result[name] = ParamInfo(
                name=name,
                original_value=(row.original_values or {}).get(name, 0),
            )
    return result


def _to_double_braces(text: str) -> str:
    """Конвертирует ``{a}`` → ``{{a}}`` для legacy-колонки ``template_text``."""
    import re

    return re.sub(r"\{(\w+)\}", r"{{\1}}", text)


def _param_details_to_constraints(details: Dict) -> Dict:
    """Конвертирует ``param_details`` → legacy ``constraints`` JSONB."""
    return {
        name: {"min": info["min"], "max": info["max"], "type": info["type"]}
        for name, info in details.items()
    }
