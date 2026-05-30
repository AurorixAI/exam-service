"""
ALGO V1 — Template Data Models
src/exam_builder/template_models.py

Dataclasses для параметрических шаблонов задач.
Используются в template_cache, llm_templatizer, variant_orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ParamInfo:
    """Информация о параметре шаблона."""

    name: str
    original_value: Any  # число из оригинальной задачи
    min_val: int = -50
    max_val: int = 50
    value_type: str = "int"  # int, float
    exclude: List[Any] = field(default_factory=list)


@dataclass
class TaskTemplate:
    """Результат шаблонизации одной задачи."""

    task_type: str  # linear, quadratic, arithmetic, ...
    text_template: str  # «Решите: {a}x + {b} = {c}»

    params: Dict[str, ParamInfo]  # {name → ParamInfo}
    original_values: Dict[str, Any]  # {name → original number}

    answer_formula_repr: str  # srepr(answer_formula) для сериализации
    answer_formula_text: str  # человеко-читаемая формула: "(c - b) / a"

    constraints_repr: List[str]  # ["a != 0", "b**2 - 4*a*c >= 0", ...]
    quality_score: float = 0.0  # 0-1 после валидации
    sympy_expr_repr: str = ""  # srepr() символьного выражения задачи

    @property
    def param_names(self) -> List[str]:
        return list(self.params.keys())
