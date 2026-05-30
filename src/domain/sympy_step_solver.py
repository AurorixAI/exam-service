"""
ALGO V1 — SymPy Step-by-Step Solver + Step-Based Distractor Engine
src/exam_builder/sympy_step_solver.py

Два класса:
  • StepSolver  — решает задачу по шагам через SymPy
  • StepDistractorEngine — генерирует дистракторы путём нарушения шагов

Покрытие типов задач:
  arithmetic, linear, quadratic, system, inequality,
  fraction, power_root, geometry, logarithm, trigonometry,
  derivative, integral, limit
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

log = logging.getLogger("sympy_engine")


# ═══════════════════════════════════════════════════════════════════════════
# Data models
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class SolutionStep:
    """Один шаг решения."""

    step_number: int
    description: str  # "Подставим значения", "Перенесём b вправо"
    expression: str  # "3x + 5 = 17"  (текст)
    result: str  # "3x = 12"  (текст после выполнения шага)
    sympy_repr: str = ""  # srepr() SymPy-выражения для программного доступа


@dataclass
class Distractor:
    """Дистрактор, сгенерированный через нарушение шага."""

    value: str  # числовой/текстовый ответ
    error_type: str  # sign_error, wrong_op, partial_result, coeff_error, ...
    broken_step: int  # номер шага, в котором допущена ошибка
    plausibility: float  # 0-1, чем позже шаг — тем выше
    explanation: str  # "Забыл поменять знак при переносе"


# ═══════════════════════════════════════════════════════════════════════════
# Step Solver
# ═══════════════════════════════════════════════════════════════════════════


class StepSolver:
    """
    Решает задачу по шагам через SymPy.

    Для каждого task_type — своя цепочка шагов.
    """

    def solve(
        self,
        task_type: str,
        formula_text: str,
        values: Dict[str, Any],
        text_template: str = "",
    ) -> List[SolutionStep]:
        """
        Решает задачу по шагам.

        Args:
            task_type: тип задачи (linear, quadratic, ...)
            formula_text: формула ответа ("(c - b) / a")
            values: конкретные значения параметров {"a": 3, "b": 5, "c": 17}
            text_template: текстовый шаблон (для Step 0)

        Returns:
            Список SolutionStep
        """
        solver = _STEP_SOLVERS.get(task_type, _steps_generic)
        try:
            return solver(formula_text, values, text_template)
        except Exception as exc:
            log.debug("StepSolver failed for %s: %s", task_type, exc)
            return _steps_generic(formula_text, values, text_template)

    def compute_answer(
        self,
        formula_text: str,
        values: Dict[str, Any],
    ) -> Optional[float]:
        """Вычисляет числовой ответ по формуле с подстановкой."""
        try:
            from sympy import sympify, N as sympy_N

            expr = sympify(formula_text)
            syms = {k: sympify(v) for k, v in values.items()}
            result = float(sympy_N(expr.subs(syms)))
            if math.isnan(result) or math.isinf(result):
                return None
            return result
        except Exception:
            return None


# ═══════════════════════════════════════════════════════════════════════════
# Per-type step solvers
# ═══════════════════════════════════════════════════════════════════════════


def _steps_generic(
    formula_text: str,
    values: Dict[str, Any],
    text_template: str = "",
) -> List[SolutionStep]:
    """Generic: подставляем → вычисляем."""
    from sympy import sympify, N as sympy_N, srepr

    steps: List[SolutionStep] = []

    # Step 0: условие
    if text_template:
        try:
            problem = text_template.format(**values)
        except Exception:
            problem = text_template
        steps.append(
            SolutionStep(
                step_number=0,
                description="Условие задачи",
                expression=problem,
                result=problem,
            )
        )

    # Step 1: запишем формулу
    steps.append(
        SolutionStep(
            step_number=1,
            description="Формула",
            expression=formula_text,
            result=formula_text,
        )
    )

    # Step 2: подставим значения
    substituted = formula_text
    for k, v in values.items():
        substituted = substituted.replace(k, str(v))
    steps.append(
        SolutionStep(
            step_number=2,
            description="Подставим значения",
            expression=substituted,
            result=substituted,
        )
    )

    # Step 3: вычислим
    try:
        expr = sympify(formula_text)
        syms = {k: sympify(v) for k, v in values.items()}
        result = sympy_N(expr.subs(syms))
        result_str = _format_number(float(result))
        steps.append(
            SolutionStep(
                step_number=3,
                description="Вычислим результат",
                expression=substituted,
                result=result_str,
                sympy_repr=srepr(result),
            )
        )
    except Exception:
        pass

    return steps


def _steps_linear(
    formula_text: str,
    values: Dict[str, Any],
    text_template: str = "",
) -> List[SolutionStep]:
    """Линейное уравнение: ax + b = c → x = (c-b)/a."""
    from sympy import sympify, srepr

    steps: List[SolutionStep] = []
    pnames = list(values.keys())

    if len(pnames) < 3:
        return _steps_generic(formula_text, values, text_template)

    a_name, b_name, c_name = pnames[0], pnames[1], pnames[2]
    a_val, b_val, c_val = values[a_name], values[b_name], values[c_name]

    # Step 0: условие
    steps.append(
        SolutionStep(
            step_number=0,
            description="Запишем уравнение",
            expression=f"{a_val}x + ({b_val}) = {c_val}",
            result=f"{a_val}x + ({b_val}) = {c_val}",
        )
    )

    # Step 1: перенесём свободный член
    rhs = c_val - b_val
    steps.append(
        SolutionStep(
            step_number=1,
            description=f"Перенесём {b_val} вправо (меняем знак)",
            expression=f"{a_val}x = {c_val} - ({b_val})",
            result=f"{a_val}x = {rhs}",
        )
    )

    # Step 2: разделим на коэффициент
    if a_val != 0:
        x_val = rhs / a_val
        x_str = _format_number(x_val)
        steps.append(
            SolutionStep(
                step_number=2,
                description=f"Разделим обе части на {a_val}",
                expression=f"x = {rhs} / {a_val}",
                result=f"x = {x_str}",
            )
        )

        # Step 3: ответ
        steps.append(
            SolutionStep(
                step_number=3,
                description="Ответ",
                expression=f"x = {x_str}",
                result=x_str,
                sympy_repr=srepr(sympify(x_val)),
            )
        )

    return steps


def _steps_quadratic(
    formula_text: str,
    values: Dict[str, Any],
    text_template: str = "",
) -> List[SolutionStep]:
    """Квадратное уравнение: дискриминант → корни."""

    steps: List[SolutionStep] = []
    pnames = list(values.keys())

    # Определяем a, b, c
    if len(pnames) >= 3:
        a_val, b_val, c_val = values[pnames[0]], values[pnames[1]], values[pnames[2]]
    elif len(pnames) >= 2:
        a_val = 1
        b_val, c_val = values[pnames[0]], values[pnames[1]]
    else:
        return _steps_generic(formula_text, values, text_template)

    # Step 0: уравнение
    if a_val == 1:
        eq_str = f"x² + ({b_val})x + ({c_val}) = 0"
    else:
        eq_str = f"{a_val}x² + ({b_val})x + ({c_val}) = 0"
    steps.append(
        SolutionStep(
            step_number=0,
            description="Запишем уравнение",
            expression=eq_str,
            result=eq_str,
        )
    )

    # Step 1: дискриминант
    D = b_val * b_val - 4 * a_val * c_val
    steps.append(
        SolutionStep(
            step_number=1,
            description="Вычислим дискриминант D = b² − 4ac",
            expression=f"D = ({b_val})² − 4·{a_val}·({c_val})",
            result=f"D = {_format_number(D)}",
        )
    )

    if D < 0:
        steps.append(
            SolutionStep(
                step_number=2,
                description="D < 0, уравнение не имеет действительных корней",
                expression=f"D = {_format_number(D)} < 0",
                result="Нет действительных корней",
            )
        )
        return steps

    # Step 2: sqrt(D)
    sqrt_D = math.sqrt(D)
    steps.append(
        SolutionStep(
            step_number=2,
            description="Вычислим √D",
            expression=f"√D = √{_format_number(D)}",
            result=f"√D = {_format_number(sqrt_D)}",
        )
    )

    # Step 3: корни
    x1 = (-b_val + sqrt_D) / (2 * a_val)
    x2 = (-b_val - sqrt_D) / (2 * a_val)
    steps.append(
        SolutionStep(
            step_number=3,
            description="Найдём корни по формуле x = (−b ± √D) / 2a",
            expression=f"x₁ = (−({b_val}) + {_format_number(sqrt_D)}) / (2·{a_val})",
            result=f"x₁ = {_format_number(x1)}, x₂ = {_format_number(x2)}",
        )
    )

    # Step 4: ответ
    answer = f"x₁ = {_format_number(min(x1, x2))}, x₂ = {_format_number(max(x1, x2))}"
    steps.append(
        SolutionStep(
            step_number=4,
            description="Ответ",
            expression=answer,
            result=_format_number(min(x1, x2))
            if "Min" in formula_text or "min" in formula_text
            else _format_number(D)
            if "**2 - 4" in formula_text
            else _format_number(max(x1, x2)),
            sympy_repr=str(min(x1, x2)),
        )
    )

    return steps


def _steps_geometry(
    formula_text: str,
    values: Dict[str, Any],
    text_template: str = "",
) -> List[SolutionStep]:
    """Геометрия: формула → подстановка → ответ."""
    from sympy import sympify, N as sympy_N, srepr

    steps: List[SolutionStep] = []

    # Step 0: условие
    if text_template:
        try:
            problem = text_template.format(**values)
        except Exception:
            problem = text_template
        steps.append(
            SolutionStep(
                step_number=0,
                description="Условие задачи",
                expression=problem,
                result=problem,
            )
        )

    # Step 1: формула
    desc = "Запишем формулу"
    if "/ 2" in formula_text or "/2" in formula_text:
        desc = "Формула площади: S = a·h / 2"
    elif "sqrt" in formula_text:
        desc = "Теорема Пифагора: c = √(a² + b²)"
    elif "*" in formula_text and "+" in formula_text:
        desc = "Формула периметра: P = 2(a + b)"

    steps.append(
        SolutionStep(
            step_number=1,
            description=desc,
            expression=formula_text,
            result=formula_text,
        )
    )

    # Step 2: подставим
    substituted = formula_text
    for k, v in values.items():
        substituted = substituted.replace(k, str(v))
    steps.append(
        SolutionStep(
            step_number=2,
            description="Подставим значения",
            expression=substituted,
            result=substituted,
        )
    )

    # Step 3: вычислим
    try:
        expr = sympify(formula_text)
        syms = {k: sympify(v) for k, v in values.items()}
        result = float(sympy_N(expr.subs(syms)))
        result_str = _format_number(result)
        steps.append(
            SolutionStep(
                step_number=3,
                description="Вычислим результат",
                expression=substituted,
                result=result_str,
                sympy_repr=srepr(sympify(result)),
            )
        )
    except Exception:
        pass

    return steps


def _steps_logarithm(
    formula_text: str,
    values: Dict[str, Any],
    text_template: str = "",
) -> List[SolutionStep]:
    """Логарифм: определение → вычисление."""
    from sympy import sympify, N as sympy_N, srepr

    steps: List[SolutionStep] = []

    if text_template:
        try:
            problem = text_template.format(**values)
        except Exception:
            problem = text_template
        steps.append(
            SolutionStep(
                step_number=0,
                description="Условие",
                expression=problem,
                result=problem,
            )
        )

    steps.append(
        SolutionStep(
            step_number=1,
            description="Применим определение логарифма: logₐ(b) = c ⟺ aᶜ = b",
            expression=formula_text,
            result=formula_text,
        )
    )

    # Подстановка
    substituted = formula_text
    for k, v in values.items():
        substituted = substituted.replace(k, str(v))

    steps.append(
        SolutionStep(
            step_number=2,
            description="Подставим значения",
            expression=substituted,
            result=substituted,
        )
    )

    try:
        expr = sympify(formula_text)
        syms = {k: sympify(v) for k, v in values.items()}
        result = float(sympy_N(expr.subs(syms)))
        steps.append(
            SolutionStep(
                step_number=3,
                description="Вычислим",
                expression=substituted,
                result=_format_number(result),
                sympy_repr=srepr(sympify(result)),
            )
        )
    except Exception:
        pass

    return steps


# ─────────────────────────────────────────────────────────────
# Fraction solver (5-6 класс)
# ─────────────────────────────────────────────────────────────


def _steps_fraction(
    formula_text: str,
    values: Dict[str, Any],
    text_template: str = "",
) -> List[SolutionStep]:
    """Дроби: приведение к общему знаменателю → операция → сокращение."""
    from sympy import Rational

    steps: List[SolutionStep] = []

    if text_template:
        try:
            problem = text_template.format(**values)
        except Exception:
            problem = text_template
        steps.append(SolutionStep(0, "Условие задачи", problem, problem))

    # Попробуем распознать a/b ± c/d
    pnames = list(values.keys())
    if len(pnames) >= 4:
        a, b, c, d = (values[pnames[i]] for i in range(4))
        if b != 0 and d != 0:
            # Step 1: записать дроби
            steps.append(
                SolutionStep(
                    1,
                    "Запишем дроби",
                    f"{a}/{b} и {c}/{d}",
                    f"{a}/{b} и {c}/{d}",
                )
            )

            # Step 2: общий знаменатель
            from math import gcd as math_gcd

            lcm_bd = abs(b * d) // math_gcd(abs(int(b)), abs(int(d)))
            mult_1 = lcm_bd // int(b)
            mult_2 = lcm_bd // int(d)
            new_a = int(a) * mult_1
            new_c = int(c) * mult_2

            steps.append(
                SolutionStep(
                    2,
                    f"Приведём к общему знаменателю {lcm_bd}",
                    f"{a}×{mult_1}/{b}×{mult_1} и {c}×{mult_2}/{d}×{mult_2}",
                    f"{new_a}/{lcm_bd} и {new_c}/{lcm_bd}",
                )
            )

            # Step 3: выполним операцию
            # Определяем операцию из формулы
            if "-" in formula_text or "sub" in formula_text.lower():
                result_num = new_a - new_c
                op_desc = "Вычтем числители"
                op_sym = "−"
            else:
                result_num = new_a + new_c
                op_desc = "Сложим числители"
                op_sym = "+"

            steps.append(
                SolutionStep(
                    3,
                    op_desc,
                    f"({new_a} {op_sym} {new_c}) / {lcm_bd}",
                    f"{result_num}/{lcm_bd}",
                )
            )

            # Step 4: сократим

            g = math_gcd(abs(result_num), abs(int(lcm_bd)))
            final_num = result_num // g
            final_den = int(lcm_bd) // g

            if g > 1:
                steps.append(
                    SolutionStep(
                        4,
                        f"Сократим на {g}",
                        f"{result_num}/{lcm_bd} = {final_num}/{final_den}",
                        f"{final_num}/{final_den}"
                        if final_den != 1
                        else str(final_num),
                        sympy_repr=str(Rational(final_num, final_den)),
                    )
                )
            else:
                steps.append(
                    SolutionStep(
                        4,
                        "Ответ",
                        f"{result_num}/{lcm_bd}",
                        f"{final_num}/{final_den}"
                        if final_den != 1
                        else str(final_num),
                        sympy_repr=str(Rational(final_num, final_den)),
                    )
                )

            return steps

    # Fallback
    return _steps_generic(formula_text, values, text_template)


# ─────────────────────────────────────────────────────────────
# Power / Root solver (7-8 класс)
# ─────────────────────────────────────────────────────────────


def _steps_power_root(
    formula_text: str,
    values: Dict[str, Any],
    text_template: str = "",
) -> List[SolutionStep]:
    """Степени и корни: правило степеней → упрощение → вычисление."""
    from sympy import sympify, N as sympy_N, srepr

    steps: List[SolutionStep] = []

    if text_template:
        try:
            problem = text_template.format(**values)
        except Exception:
            problem = text_template
        steps.append(SolutionStep(0, "Условие задачи", problem, problem))

    # Step 1: определить правило
    rule_desc = "Применим правило степеней"
    if "**" in formula_text and "+" in formula_text:
        rule_desc = "Правило: a^m · a^n = a^(m+n)"
    elif "**" in formula_text and "-" in formula_text:
        rule_desc = "Правило: a^m / a^n = a^(m−n)"
    elif (
        "sqrt" in formula_text.lower()
        or "**0.5" in formula_text
        or "**(1/2)" in formula_text
    ):
        rule_desc = "Правило: √(a^n) = a^(n/2)"
    elif "**" in formula_text:
        rule_desc = "Правило: (a^m)^n = a^(m·n)"

    steps.append(SolutionStep(1, rule_desc, formula_text, formula_text))

    # Step 2: подстановка
    substituted = formula_text
    for k, v in values.items():
        substituted = substituted.replace(k, str(v))
    steps.append(SolutionStep(2, "Подставим значения", substituted, substituted))

    # Step 3: вычисление
    try:
        expr = sympify(formula_text)
        syms = {k: sympify(v) for k, v in values.items()}
        result = float(sympy_N(expr.subs(syms)))
        result_str = _format_number(result)
        steps.append(
            SolutionStep(
                3,
                "Вычислим результат",
                substituted,
                result_str,
                sympy_repr=srepr(sympify(result)),
            )
        )
    except Exception:
        pass

    return steps


# ─────────────────────────────────────────────────────────────
# System of equations solver (7-9 класс)
# ─────────────────────────────────────────────────────────────


def _steps_system(
    formula_text: str,
    values: Dict[str, Any],
    text_template: str = "",
) -> List[SolutionStep]:
    """Система уравнений: метод подстановки."""

    steps: List[SolutionStep] = []

    if text_template:
        try:
            problem = text_template.format(**values)
        except Exception:
            problem = text_template
        steps.append(SolutionStep(0, "Условие", problem, problem))

    pnames = list(values.keys())

    # Типичная система: a1*x + b1*y = c1, a2*x + b2*y = c2
    if len(pnames) >= 6:
        a1, b1, c1 = values[pnames[0]], values[pnames[1]], values[pnames[2]]
        a2, b2, c2 = values[pnames[3]], values[pnames[4]], values[pnames[5]]

        # Step 1: записать систему
        steps.append(
            SolutionStep(
                1,
                "Система уравнений",
                f"{a1}x + {b1}y = {c1}\n{a2}x + {b2}y = {c2}",
                f"{a1}x + {b1}y = {c1}, {a2}x + {b2}y = {c2}",
            )
        )

        # Step 2: выразить y из первого
        if b1 != 0:
            steps.append(
                SolutionStep(
                    2,
                    "Выразим y из первого уравнения",
                    f"{b1}y = {c1} − {a1}x",
                    f"y = ({c1} − {a1}x) / {b1}",
                )
            )

            # Step 3: подставить во второе
            # a2*x + b2*((c1 - a1*x)/b1) = c2
            steps.append(
                SolutionStep(
                    3,
                    "Подставим во второе уравнение",
                    f"{a2}x + {b2}·({c1} − {a1}x)/{b1} = {c2}",
                    "Решаем относительно x",
                )
            )

            # Step 4: найти x
            det = a1 * b2 - a2 * b1
            if det != 0:
                x_val = (c1 * b2 - c2 * b1) / det
                y_val = (a1 * c2 - a2 * c1) / det
                steps.append(
                    SolutionStep(
                        4,
                        "Решаем: x = (c₁b₂ − c₂b₁) / (a₁b₂ − a₂b₁)",
                        f"x = ({c1}·{b2} − {c2}·{b1}) / ({a1}·{b2} − {a2}·{b1})",
                        f"x = {_format_number(x_val)}",
                    )
                )

                # Step 5: найти y
                steps.append(
                    SolutionStep(
                        5,
                        "Найдём y подстановкой",
                        f"y = ({c1} − {a1}·{_format_number(x_val)}) / {b1}",
                        f"y = {_format_number(y_val)}",
                    )
                )

                # Step 6: ответ
                steps.append(
                    SolutionStep(
                        6,
                        "Ответ",
                        f"x = {_format_number(x_val)}, y = {_format_number(y_val)}",
                        f"({_format_number(x_val)}; {_format_number(y_val)})",
                        sympy_repr=f"({x_val}, {y_val})",
                    )
                )

                return steps

    return _steps_generic(formula_text, values, text_template)


# ─────────────────────────────────────────────────────────────
# Inequality solver (8-9 класс)
# ─────────────────────────────────────────────────────────────


def _steps_inequality(
    formula_text: str,
    values: Dict[str, Any],
    text_template: str = "",
) -> List[SolutionStep]:
    """Неравенство: перенос → деление (с учётом знака!)."""

    steps: List[SolutionStep] = []

    if text_template:
        try:
            problem = text_template.format(**values)
        except Exception:
            problem = text_template
        steps.append(SolutionStep(0, "Условие", problem, problem))

    pnames = list(values.keys())

    # Линейное неравенство: ax + b > c (или <, >=, <=)
    if len(pnames) >= 3:
        a_val, b_val, c_val = values[pnames[0]], values[pnames[1]], values[pnames[2]]

        # Определяем знак неравенства из формулы
        ineq_sign = ">"
        for sign in [">=", "<=", ">", "<"]:
            if sign in formula_text:
                ineq_sign = sign
                break

        steps.append(
            SolutionStep(
                1,
                "Запишем неравенство",
                f"{a_val}x + ({b_val}) {ineq_sign} {c_val}",
                f"{a_val}x + ({b_val}) {ineq_sign} {c_val}",
            )
        )

        # Step 2: перенос
        rhs = c_val - b_val
        steps.append(
            SolutionStep(
                2,
                f"Перенесём {b_val} вправо (меняем знак)",
                f"{a_val}x {ineq_sign} {c_val} − ({b_val})",
                f"{a_val}x {ineq_sign} {_format_number(rhs)}",
            )
        )

        # Step 3: деление — КЛЮЧЕВОЙ МОМЕНТ
        if a_val != 0:
            x_val = rhs / a_val
            # При делении на отрицательное — знак переворачивается!
            if a_val < 0:
                flipped = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}.get(
                    ineq_sign, ineq_sign
                )
                steps.append(
                    SolutionStep(
                        3,
                        f"Делим на {a_val} — число отрицательное, ПЕРЕВОРАЧИВАЕМ знак!",
                        f"x {flipped} {_format_number(rhs)} / ({a_val})",
                        f"x {flipped} {_format_number(x_val)}",
                    )
                )
                final_sign = flipped
            else:
                steps.append(
                    SolutionStep(
                        3,
                        f"Делим обе части на {a_val}",
                        f"x {ineq_sign} {_format_number(rhs)} / {a_val}",
                        f"x {ineq_sign} {_format_number(x_val)}",
                    )
                )
                final_sign = ineq_sign

            # Step 4: ответ
            steps.append(
                SolutionStep(
                    4,
                    "Ответ",
                    f"x {final_sign} {_format_number(x_val)}",
                    _format_number(x_val),
                    sympy_repr=str(x_val),
                )
            )

            return steps

    return _steps_generic(formula_text, values, text_template)


# ─────────────────────────────────────────────────────────────
# Trigonometry solver (9-11 класс)
# ─────────────────────────────────────────────────────────────


def _steps_trigonometry(
    formula_text: str,
    values: Dict[str, Any],
    text_template: str = "",
) -> List[SolutionStep]:
    """Тригонометрия: определить четверть → базовый угол → знак → ответ."""
    from sympy import sympify, N as sympy_N, srepr

    steps: List[SolutionStep] = []

    if text_template:
        try:
            problem = text_template.format(**values)
        except Exception:
            problem = text_template
        steps.append(SolutionStep(0, "Условие", problem, problem))

    # Step 1: определить функцию и аргумент
    func_name = "sin"
    for fn in ["sin", "cos", "tan", "cot"]:
        if fn in formula_text.lower():
            func_name = fn
            break

    steps.append(
        SolutionStep(
            1,
            f"Определим тригонометрическую функцию: {func_name}",
            formula_text,
            formula_text,
        )
    )

    # Step 2: подставить значения
    substituted = formula_text
    for k, v in values.items():
        substituted = substituted.replace(k, str(v))

    steps.append(
        SolutionStep(
            2,
            "Подставим значения угла",
            substituted,
            substituted,
        )
    )

    # Step 3: определить четверть (если угол в градусах)
    pnames = list(values.keys())
    if pnames:
        angle = values[pnames[0]]
        try:
            angle_val = float(angle)
            quadrant = 1 + int((angle_val % 360) // 90)
            if quadrant > 4:
                quadrant = 4
            steps.append(
                SolutionStep(
                    3,
                    f"Угол {_format_number(angle_val)}° находится в {quadrant}-й четверти",
                    f"Четверть: {quadrant}",
                    f"Четверть: {quadrant}",
                )
            )
        except (ValueError, TypeError):
            pass

    # Step 4: вычислить
    try:
        expr = sympify(formula_text)
        syms = {k: sympify(v) for k, v in values.items()}
        result = float(sympy_N(expr.subs(syms)))
        result_str = _format_number(result)
        steps.append(
            SolutionStep(
                4,
                "Вычислим значение",
                substituted,
                result_str,
                sympy_repr=srepr(sympify(result)),
            )
        )
    except Exception:
        pass

    return steps


# ─────────────────────────────────────────────────────────────
# Derivative solver (10-11 класс)
# ─────────────────────────────────────────────────────────────


def _steps_derivative(
    formula_text: str,
    values: Dict[str, Any],
    text_template: str = "",
) -> List[SolutionStep]:
    """Производная: определить правило → применить → упростить."""
    from sympy import sympify, diff, symbols, N as sympy_N, srepr

    steps: List[SolutionStep] = []

    if text_template:
        try:
            problem = text_template.format(**values)
        except Exception:
            problem = text_template
        steps.append(SolutionStep(0, "Условие", problem, problem))

    # Step 1: записать функцию
    substituted = formula_text
    for k, v in values.items():
        substituted = substituted.replace(k, str(v))

    steps.append(
        SolutionStep(
            1,
            "Запишем функцию f(x)",
            f"f(x) = {substituted}",
            f"f(x) = {substituted}",
        )
    )

    # Step 2: определить правило дифференцирования
    rule = "Применим правила дифференцирования"
    if "*x**" in substituted or "x**" in substituted:
        rule = "Правило: (xⁿ)' = n·x^(n−1)"
    if "sin" in substituted:
        rule = "Правило: (sin x)' = cos x"
    if "cos" in substituted:
        rule = "Правило: (cos x)' = −sin x"
    if "exp" in substituted or "E**" in substituted:
        rule = "Правило: (eˣ)' = eˣ"
    if "log" in substituted or "ln" in substituted:
        rule = "Правило: (ln x)' = 1/x"

    steps.append(SolutionStep(2, rule, substituted, substituted))

    # Step 3: вычислить производную через SymPy
    try:
        x = symbols("x")
        expr = sympify(substituted)
        deriv = diff(expr, x)
        deriv_str = str(deriv)

        steps.append(
            SolutionStep(
                3,
                "Вычислим производную",
                f"f'(x) = d/dx [{substituted}]",
                f"f'(x) = {deriv_str}",
            )
        )

        # Step 4: если нужно вычислить в точке
        if "x0" in values or "point" in values:
            x0 = values.get("x0", values.get("point", 0))
            result_val = float(sympy_N(deriv.subs(x, x0)))
            steps.append(
                SolutionStep(
                    4,
                    f"Вычислим f'({x0})",
                    f"f'({x0}) = {deriv_str} при x={x0}",
                    _format_number(result_val),
                    sympy_repr=srepr(sympify(result_val)),
                )
            )
        else:
            # Финальный ответ — формула производной
            try:
                result_val = float(sympy_N(deriv))
                steps.append(
                    SolutionStep(
                        4,
                        "Ответ",
                        f"f'(x) = {deriv_str}",
                        _format_number(result_val),
                        sympy_repr=srepr(deriv),
                    )
                )
            except Exception:
                steps.append(
                    SolutionStep(
                        4,
                        "Ответ",
                        f"f'(x) = {deriv_str}",
                        deriv_str,
                        sympy_repr=srepr(deriv),
                    )
                )
    except Exception:
        pass

    return steps


# ─────────────────────────────────────────────────────────────
# Integral solver (11 класс)
# ─────────────────────────────────────────────────────────────


def _steps_integral(
    formula_text: str,
    values: Dict[str, Any],
    text_template: str = "",
) -> List[SolutionStep]:
    """Интеграл: определить тип → первообразная → подставить пределы."""
    from sympy import sympify, integrate, symbols, N as sympy_N, srepr

    steps: List[SolutionStep] = []

    if text_template:
        try:
            problem = text_template.format(**values)
        except Exception:
            problem = text_template
        steps.append(SolutionStep(0, "Условие", problem, problem))

    substituted = formula_text
    for k, v in values.items():
        substituted = substituted.replace(k, str(v))

    steps.append(
        SolutionStep(
            1,
            "Запишем подынтегральное выражение",
            f"∫ {substituted} dx",
            f"∫ {substituted} dx",
        )
    )

    # Step 2: правило интегрирования
    rule = "Применим правила интегрирования"
    if "x**" in substituted:
        rule = "Правило: ∫xⁿ dx = x^(n+1)/(n+1) + C"
    if "sin" in substituted:
        rule = "Правило: ∫sin x dx = −cos x + C"
    if "cos" in substituted:
        rule = "Правило: ∫cos x dx = sin x + C"
    if "exp" in substituted or "E**" in substituted:
        rule = "Правило: ∫eˣ dx = eˣ + C"

    steps.append(SolutionStep(2, rule, substituted, substituted))

    try:
        x = symbols("x")
        expr = sympify(substituted)

        # Определённый интеграл (если есть пределы)
        lower = values.get("lower", values.get("a_lim", None))
        upper = values.get("upper", values.get("b_lim", None))

        if lower is not None and upper is not None:
            # Определённый интеграл
            antideriv = integrate(expr, x)
            antideriv_str = str(antideriv)

            steps.append(
                SolutionStep(
                    3,
                    "Найдём первообразную F(x)",
                    f"F(x) = {antideriv_str}",
                    f"F(x) = {antideriv_str}",
                )
            )

            # Step 4: подставить пределы
            f_upper = float(sympy_N(antideriv.subs(x, upper)))
            f_lower = float(sympy_N(antideriv.subs(x, lower)))
            result_val = f_upper - f_lower

            steps.append(
                SolutionStep(
                    4,
                    f"Подставим пределы: F({upper}) − F({lower})",
                    f"{_format_number(f_upper)} − ({_format_number(f_lower)})",
                    _format_number(result_val),
                    sympy_repr=srepr(sympify(result_val)),
                )
            )
        else:
            # Неопределённый интеграл
            result = integrate(expr, x)
            result_str = f"{result} + C"

            steps.append(
                SolutionStep(
                    3,
                    "Найдём первообразную",
                    f"∫ {substituted} dx",
                    result_str,
                    sympy_repr=srepr(result),
                )
            )

            try:
                result_val = float(sympy_N(result))
                steps.append(
                    SolutionStep(
                        4,
                        "Ответ",
                        result_str,
                        _format_number(result_val),
                    )
                )
            except Exception:
                steps.append(
                    SolutionStep(
                        4,
                        "Ответ",
                        result_str,
                        result_str,
                    )
                )
    except Exception:
        pass

    return steps


# ─────────────────────────────────────────────────────────────
# Limit solver (10-11 класс)
# ─────────────────────────────────────────────────────────────


def _steps_limit(
    formula_text: str,
    values: Dict[str, Any],
    text_template: str = "",
) -> List[SolutionStep]:
    """Предел: подстановка → если неопределённость → раскрытие."""
    from sympy import sympify, limit as sym_limit, symbols, N as sympy_N, oo, srepr

    steps: List[SolutionStep] = []

    if text_template:
        try:
            problem = text_template.format(**values)
        except Exception:
            problem = text_template
        steps.append(SolutionStep(0, "Условие", problem, problem))

    substituted = formula_text
    for k, v in values.items():
        substituted = substituted.replace(k, str(v))

    # Точка предела
    x0 = values.get("x0", values.get("point", 0))
    to_inf = str(x0).lower() in ("inf", "oo", "infinity")

    steps.append(
        SolutionStep(
            1,
            f"Найдём предел при x → {'∞' if to_inf else x0}",
            f"lim (x→{'∞' if to_inf else x0}) [{substituted}]",
            f"lim (x→{'∞' if to_inf else x0}) [{substituted}]",
        )
    )

    try:
        x = symbols("x")
        expr = sympify(substituted)

        # Step 2: попробуем прямую подстановку
        if not to_inf:
            try:
                direct = float(sympy_N(expr.subs(x, x0)))
                if math.isfinite(direct):
                    steps.append(
                        SolutionStep(
                            2,
                            f"Подставим x = {x0} напрямую",
                            f"f({x0}) = {substituted} при x={x0}",
                            _format_number(direct),
                        )
                    )
                    steps.append(
                        SolutionStep(
                            3,
                            "Прямая подстановка дала конечный результат",
                            _format_number(direct),
                            _format_number(direct),
                            sympy_repr=srepr(sympify(direct)),
                        )
                    )
                    return steps
                else:
                    steps.append(
                        SolutionStep(
                            2,
                            f"Прямая подстановка x = {x0} даёт неопределённость",
                            "0/0 или ∞/∞",
                            "Нужно раскрыть неопределённость",
                        )
                    )
            except Exception:
                steps.append(
                    SolutionStep(
                        2,
                        "Прямая подстановка невозможна",
                        "Неопределённость",
                        "Применим правило Лопиталя или алгебраические преобразования",
                    )
                )

        # Step 3: вычислить через SymPy limit
        point = oo if to_inf else sympify(x0)
        result = sym_limit(expr, x, point)
        result_val = float(sympy_N(result))

        steps.append(
            SolutionStep(
                3,
                "Вычислим предел"
                + (" (правило Лопиталя)" if not to_inf else " (при x→∞)"),
                f"lim = {result}",
                _format_number(result_val),
                sympy_repr=srepr(result),
            )
        )

        steps.append(
            SolutionStep(
                4,
                "Ответ",
                _format_number(result_val),
                _format_number(result_val),
                sympy_repr=srepr(result),
            )
        )

    except Exception:
        pass

    return steps


# Solver registry
_STEP_SOLVERS = {
    "arithmetic": _steps_generic,
    "linear": _steps_linear,
    "quadratic": _steps_quadratic,
    "geometry": _steps_geometry,
    "logarithm": _steps_logarithm,
    "fraction": _steps_fraction,
    "power_root": _steps_power_root,
    "system": _steps_system,
    "inequality": _steps_inequality,
    "trigonometry": _steps_trigonometry,
    "derivative": _steps_derivative,
    "integral": _steps_integral,
    "limit": _steps_limit,
}


# ═══════════════════════════════════════════════════════════════════════════
# Step-Based Distractor Engine
# ═══════════════════════════════════════════════════════════════════════════


class StepDistractorEngine:
    """
    Генерирует дистракторы путём «нарушения» шагов решения.

    Принцип: берём цепочку шагов, на каждом шаге вносим типичную ошибку,
    досчитываем до конца — получаем правдоподобный неправильный ответ.

    Plausibility = 0.5 + 0.5 * (broken_step / total_steps)
    Чем позже ошибка — тем выше plausibility (ученик дальше продвинулся).

    Поддерживаемые типы ответов:
    - exact_number / decimal  → float-based стратегии (6 + fallback)
    - fraction (3/4)          → SymPy Rational: перевернуть, ±1 числитель/знаменатель, не сократить
    - expression (2x+3)       → SymPy parse_expr: ±коэффициент, потеря члена, ошибка степени
    - multiple_values (x1=-2, x2=3) → потеря корня, ошибка знака одного корня
    - open_text               → пустой список (обрабатывается AI-генератором)
    """

    # ─────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────

    def generate_for_answer(
        self,
        correct_answer: str,
        answer_type: str = "exact_number",
        steps: Optional[List[SolutionStep]] = None,
        task_type: str = "arithmetic",
        count: int = 3,
    ) -> List[Distractor]:
        """
        Единый вход: генерирует дистракторы для ЛЮБОГО типа ответа.

        Маршрутизация:
            exact_number / decimal  → generate() (float-based, по шагам)
            fraction                → _fraction_distractors()
            expression              → _expression_distractors()
            multiple_values         → _multi_value_distractors()
            open_text / text        → [] (обрабатывается AIDistractorGenerator)
        """
        answer_type = (answer_type or "exact_number").lower().strip()
        correct = (correct_answer or "").strip()
        if not correct:
            return []

        if answer_type in ("exact_number", "decimal"):
            try:
                val = float(correct.replace(",", "."))
                return self.generate(steps or [], val, task_type, count)
            except ValueError:
                return []

        if answer_type == "fraction":
            return self._fraction_distractors(correct, count)

        if answer_type in ("expression", "equation_solution", "inequality"):
            return self._expression_distractors(correct, count)

        if answer_type == "multiple_values":
            return self._multi_value_distractors(correct, count)

        # open_text, text, true_false, multiple_choice → пусто (AI или не нужно)
        return []

    def generate(
        self,
        steps: List[SolutionStep],
        correct_answer: float,
        task_type: str = "arithmetic",
        count: int = 3,
    ) -> List[Distractor]:
        """
        Генерирует дистракторы из шагов решения.

        Args:
            steps: шаги решения от StepSolver
            correct_answer: правильный ответ (число)
            task_type: тип задачи
            count: сколько нужно дистракторов

        Returns:
            Список Distractor, отсортированных по plausibility
        """
        results: List[Distractor] = []
        seen_values: set = {_format_number(correct_answer)}
        total_steps = max(len(steps), 1)

        # Стратегии нарушения
        strategies = [
            self._sign_error_at_step,
            self._wrong_operation,
            self._partial_result,
            self._coefficient_error,
            self._formula_error,
            self._simplification_error,
        ]

        for strategy in strategies:
            if len(results) >= count:
                break
            try:
                distractors = strategy(steps, correct_answer, task_type, total_steps)
                for d in distractors:
                    norm = (
                        _format_number(float(d.value))
                        if _is_number(d.value)
                        else d.value
                    )
                    if norm not in seen_values:
                        seen_values.add(norm)
                        results.append(d)
                        if len(results) >= count:
                            break
            except Exception:
                continue

        # Fallback: арифметические дистракторы
        if len(results) < count:
            fallbacks = self._arithmetic_fallback(
                correct_answer, count - len(results), seen_values
            )
            results.extend(fallbacks)

        results.sort(key=lambda d: d.plausibility, reverse=True)
        return results[:count]

    # ─────────────────────────────────────────────────────────
    # Strategy: sign error at transfer step
    # ─────────────────────────────────────────────────────────

    def _sign_error_at_step(
        self,
        steps: List[SolutionStep],
        correct: float,
        task_type: str,
        total: int,
    ) -> List[Distractor]:
        """Ошибка знака при переносе через «=»."""
        results: List[Distractor] = []

        # Находим шаги с переносом
        for step in steps:
            if (
                "перенес" in step.description.lower()
                or "знак" in step.description.lower()
            ):
                # Обратный знак
                wrong = -correct if correct != 0 else 1
                plaus = 0.5 + 0.5 * (step.step_number / total)
                results.append(
                    Distractor(
                        value=_format_number(wrong),
                        error_type="sign_transfer_error",
                        broken_step=step.step_number,
                        plausibility=min(plaus, 0.95),
                        explanation="Забыл поменять знак при переносе слагаемого через «=»",
                    )
                )
                break

        # Также: -answer (универсальная ошибка знака)
        if correct != 0:
            results.append(
                Distractor(
                    value=_format_number(-correct),
                    error_type="sign_error",
                    broken_step=max(total - 2, 1),
                    plausibility=0.85,
                    explanation="Ошибка знака в итоговом ответе",
                )
            )

        return results

    # ─────────────────────────────────────────────────────────
    # Strategy: wrong operation
    # ─────────────────────────────────────────────────────────

    def _wrong_operation(
        self,
        steps: List[SolutionStep],
        correct: float,
        task_type: str,
        total: int,
    ) -> List[Distractor]:
        """Перепутал операцию: + вместо -, * вместо /."""
        results: List[Distractor] = []

        # Ищем шаг с делением
        for step in steps:
            desc = step.description.lower()
            if "раздел" in desc or "дел" in desc:
                # Вместо деления — умножение
                nums = re.findall(r"[-+]?\d+(?:\.\d+)?", step.expression)
                if len(nums) >= 2:
                    try:
                        a, b = float(nums[0]), float(nums[1])
                        if b != 0:
                            wrong = a * b  # умножил вместо деления
                            plaus = 0.5 + 0.5 * (step.step_number / total)
                            results.append(
                                Distractor(
                                    value=_format_number(wrong),
                                    error_type="wrong_operation",
                                    broken_step=step.step_number,
                                    plausibility=min(plaus, 0.90),
                                    explanation="Умножил вместо деления",
                                )
                            )
                    except Exception:
                        pass
                break

        return results

    # ─────────────────────────────────────────────────────────
    # Strategy: partial result
    # ─────────────────────────────────────────────────────────

    def _partial_result(
        self,
        steps: List[SolutionStep],
        correct: float,
        task_type: str,
        total: int,
    ) -> List[Distractor]:
        """Промежуточный результат как «ответ» — остановился раньше."""
        results: List[Distractor] = []

        for step in steps:
            if step.step_number == 0:
                continue
            # Ищем числа в result
            nums = re.findall(r"[-+]?\d+(?:\.\d+)?", step.result)
            for num_str in nums:
                try:
                    val = float(num_str)
                    if val != correct and val != 0 and abs(val) < abs(correct) * 100:
                        plaus = 0.4 + 0.3 * (step.step_number / total)
                        results.append(
                            Distractor(
                                value=_format_number(val),
                                error_type="partial_result",
                                broken_step=step.step_number,
                                plausibility=min(plaus, 0.80),
                                explanation=f"Остановился на шаге {step.step_number}: "
                                f"записал промежуточный результат как ответ",
                            )
                        )
                except Exception:
                    continue

        return results

    # ─────────────────────────────────────────────────────────
    # Strategy: coefficient error
    # ─────────────────────────────────────────────────────────

    def _coefficient_error(
        self,
        steps: List[SolutionStep],
        correct: float,
        task_type: str,
        total: int,
    ) -> List[Distractor]:
        """Потерял коэффициент: забыл умножить/разделить на 2, и т.п."""
        results: List[Distractor] = []

        for coeff, desc in [(2, "забыл разделить на 2"), (0.5, "забыл умножить на 2")]:
            wrong = correct * coeff
            if wrong != correct:
                results.append(
                    Distractor(
                        value=_format_number(wrong),
                        error_type="coefficient_error",
                        broken_step=max(total - 1, 1),
                        plausibility=0.80,
                        explanation=f"Ошибка коэффициента: {desc}",
                    )
                )

        return results

    # ─────────────────────────────────────────────────────────
    # Strategy: formula error
    # ─────────────────────────────────────────────────────────

    def _formula_error(
        self,
        steps: List[SolutionStep],
        correct: float,
        task_type: str,
        total: int,
    ) -> List[Distractor]:
        """Неправильная формула (D = b²+4ac вместо b²−4ac)."""
        results: List[Distractor] = []

        if task_type == "quadratic":
            # Ищем шаг с дискриминантом
            for step in steps:
                if (
                    "дискриминант" in step.description.lower()
                    or "D =" in step.expression
                ):
                    nums = re.findall(r"[-+]?\d+(?:\.\d+)?", step.result)
                    if nums:
                        try:
                            d_val = float(nums[-1])
                            # D = b²+4ac вместо b²−4ac → D' = D + 8ac
                            # Проще: инвертировать знак 4ac
                            wrong_d = -d_val if d_val != 0 else 4
                            results.append(
                                Distractor(
                                    value=_format_number(wrong_d),
                                    error_type="formula_error",
                                    broken_step=step.step_number,
                                    plausibility=0.88,
                                    explanation="Использовал D = b²+4ac вместо D = b²−4ac",
                                )
                            )
                        except Exception:
                            pass
                    break

        return results

    # ─────────────────────────────────────────────────────────
    # Strategy: simplification error
    # ─────────────────────────────────────────────────────────

    def _simplification_error(
        self,
        steps: List[SolutionStep],
        correct: float,
        task_type: str,
        total: int,
    ) -> List[Distractor]:
        """Ошибка при упрощении: ±1, ±2 от правильного ответа."""
        results: List[Distractor] = []

        for delta in [1, -1, 2]:
            wrong = correct + delta
            if wrong != correct:
                results.append(
                    Distractor(
                        value=_format_number(wrong),
                        error_type="simplification_error",
                        broken_step=max(total - 1, 1),
                        plausibility=0.65,
                        explanation=f"Ошибка при упрощении: ответ ±{abs(delta)}",
                    )
                )

        return results

    # ─────────────────────────────────────────────────────────
    # Non-numeric distractors: FRACTION
    # ─────────────────────────────────────────────────────────

    def _fraction_distractors(self, correct: str, count: int = 3) -> List[Distractor]:
        """
        Дистракторы для дробей (3/4, -1/2, 7/3 и т.д.)

        Стратегии:
        1. Перевернуть дробь (4/3 вместо 3/4)
        2. Не сократить (6/8 вместо 3/4)
        3. Ошибка в числителе ±1
        4. Ошибка в знаменателе ±1
        5. Ошибка знака
        """
        from fractions import Fraction as PyFraction

        results: List[Distractor] = []
        seen: set = {correct.replace(" ", "")}

        try:
            frac = PyFraction(correct.replace(" ", ""))
        except (ValueError, ZeroDivisionError):
            return []

        num, den = frac.numerator, frac.denominator

        candidates = []

        # 1. Перевернуть дробь
        if num != 0 and num != den:
            inv = PyFraction(den, num)
            candidates.append(
                (
                    str(inv),
                    "inverted_fraction",
                    0.88,
                    "Перепутал числитель и знаменатель",
                )
            )

        # 2. Не сократить — показать несокращённый вариант (×2, ×3)
        for mult in (2, 3):
            unsimplified = f"{num * mult}/{den * mult}"
            candidates.append(
                (
                    unsimplified,
                    "unsimplified_fraction",
                    0.82,
                    f"Забыл сократить дробь (×{mult})",
                )
            )

        # 3. Ошибка в числителе ±1
        for delta in (1, -1):
            wrong_num = num + delta
            if wrong_num != 0:
                wrong = PyFraction(wrong_num, den)
                candidates.append(
                    (
                        str(wrong),
                        "numerator_error",
                        0.78,
                        f"Ошибка в числителе: {num}{'+' if delta > 0 else ''}{delta}",
                    )
                )

        # 4. Ошибка в знаменателе ±1
        for delta in (1, -1):
            wrong_den = den + delta
            if wrong_den > 0:
                wrong = PyFraction(num, wrong_den)
                candidates.append(
                    (
                        str(wrong),
                        "denominator_error",
                        0.75,
                        f"Ошибка в знаменателе: {den}{'+' if delta > 0 else ''}{delta}",
                    )
                )

        # 5. Ошибка знака
        if frac != 0:
            neg = -frac
            candidates.append(
                (
                    str(neg),
                    "sign_error",
                    0.85,
                    "Ошибка знака дроби",
                )
            )

        for val_str, etype, plaus, expl in candidates:
            if len(results) >= count:
                break
            norm = val_str.replace(" ", "")
            if norm not in seen:
                seen.add(norm)
                results.append(
                    Distractor(
                        value=val_str,
                        error_type=etype,
                        broken_step=0,
                        plausibility=plaus,
                        explanation=expl,
                    )
                )

        results.sort(key=lambda d: d.plausibility, reverse=True)
        return results[:count]

    # ─────────────────────────────────────────────────────────
    # Non-numeric distractors: EXPRESSION
    # ─────────────────────────────────────────────────────────

    def _expression_distractors(self, correct: str, count: int = 3) -> List[Distractor]:
        """
        Дистракторы для выражений (2x+3, x²−5x+6, 3a−b и т.д.)

        Стратегии:
        1. Изменить знак одного члена (2x+3 → 2x−3)
        2. Ошибка в коэффициенте ±1 (2x+3 → 3x+3)
        3. Потерять последний член (x²−5x+6 → x²−5x)
        4. Ошибка в степени (x² → x³)
        5. Раскрытие скобок с ошибкой знака
        """
        from sympy import parse_expr, Poly, Add

        results: List[Distractor] = []
        seen: set = {correct.replace(" ", "")}

        try:
            expr = parse_expr(correct.replace("^", "**"))
        except Exception:
            return []

        candidates = []

        # Извлекаем свободные символы
        free_syms = sorted(expr.free_symbols, key=str)
        if not free_syms:
            # Это просто число — fallback на числовой
            try:
                return self._fraction_distractors(correct, count)
            except Exception:
                return []

        x = free_syms[0]

        # 1. Изменить знак: −expr
        neg_expr = -expr
        candidates.append(
            (
                str(neg_expr),
                "sign_error",
                0.85,
                "Ошибка знака всего выражения",
            )
        )

        # 2. Если выражение — сумма, поменять знак одного члена
        terms = Add.make_args(expr)
        if len(terms) > 1:
            for i, term in enumerate(terms):
                new_terms = list(terms)
                new_terms[i] = -term
                wrong = Add(*new_terms)
                candidates.append(
                    (
                        str(wrong),
                        "term_sign_error",
                        0.88,
                        f"Ошибка знака в члене: {term} → {-term}",
                    )
                )

        # 3. Потерять последний член
        if len(terms) > 1:
            partial = Add(*terms[:-1])
            candidates.append(
                (
                    str(partial),
                    "lost_term",
                    0.80,
                    f"Потерял член: {terms[-1]}",
                )
            )

        # 4. Ошибка в коэффициенте старшего члена ±1
        try:
            poly = Poly(expr, x)
            coeffs = poly.all_coeffs()
            if coeffs and abs(coeffs[0]) > 0:
                for delta in (1, -1):
                    new_coeffs = list(coeffs)
                    new_coeffs[0] = coeffs[0] + delta
                    if new_coeffs[0] != 0:
                        # Восстанавливаем выражение из коэффициентов
                        deg = len(coeffs) - 1
                        wrong = sum(
                            c * x**p for p, c in zip(range(deg, -1, -1), new_coeffs)
                        )
                        candidates.append(
                            (
                                str(wrong),
                                "coefficient_error",
                                0.82,
                                f"Ошибка в старшем коэффициенте: {coeffs[0]} → {coeffs[0] + delta}",
                            )
                        )
        except Exception:
            pass

        # 5. Ошибка в степени: x² → x³ (или наоборот)
        try:
            poly = Poly(expr, x)
            deg_val = poly.degree()
            if deg_val >= 2:
                wrong = expr.subs(x**deg_val, x ** (deg_val + 1))
                candidates.append(
                    (
                        str(wrong),
                        "degree_error",
                        0.75,
                        f"Ошибка в степени: x^{deg_val} → x^{deg_val + 1}",
                    )
                )
                wrong2 = expr.subs(x**deg_val, x ** (deg_val - 1))
                candidates.append(
                    (
                        str(wrong2),
                        "degree_error",
                        0.72,
                        f"Ошибка в степени: x^{deg_val} → x^{deg_val - 1}",
                    )
                )
        except Exception:
            pass

        for val_str, etype, plaus, expl in candidates:
            if len(results) >= count:
                break
            norm = val_str.replace(" ", "")
            if norm not in seen:
                seen.add(norm)
                results.append(
                    Distractor(
                        value=val_str,
                        error_type=etype,
                        broken_step=0,
                        plausibility=plaus,
                        explanation=expl,
                    )
                )

        results.sort(key=lambda d: d.plausibility, reverse=True)
        return results[:count]

    # ─────────────────────────────────────────────────────────
    # Non-numeric distractors: MULTIPLE VALUES
    # ─────────────────────────────────────────────────────────

    def _multi_value_distractors(
        self, correct: str, count: int = 3
    ) -> List[Distractor]:
        """
        Дистракторы для множественных значений (x1=-2, x2=3 / -2; 3 / -2, 3)

        Стратегии:
        1. Потерять один корень (написать только один из двух)
        2. Ошибка знака одного корня (-2, 3 → 2, 3)
        3. Перепутать корни местами с ошибкой знака (-2, 3 → -3, 2)
        """
        results: List[Distractor] = []

        # Парсим значения: "x1=-2, x2=3" / "-2; 3" / "-2, 3"
        import re

        raw = correct.replace(";", ",")
        # Извлекаем числа из строки
        numbers = re.findall(r"[-+]?\d+(?:\.\d+)?(?:/\d+)?", raw)
        if len(numbers) < 2:
            return []

        seen: set = {correct.replace(" ", "")}
        separator = "; "

        candidates = []

        # 1. Потерять один корень — записать только первый или второй
        for i, num in enumerate(numbers):
            candidates.append(
                (
                    num,
                    "lost_root",
                    0.85,
                    f"Нашёл только один корень: {num} (потерял остальные)",
                )
            )

        # 2. Ошибка знака одного корня
        for i, num_str in enumerate(numbers):
            try:
                val = float(num_str)
                if val != 0:
                    new_nums = list(numbers)
                    new_nums[i] = _format_number(-val)
                    wrong = separator.join(new_nums)
                    candidates.append(
                        (
                            wrong,
                            "sign_error_in_root",
                            0.88,
                            f"Ошибка знака в корне: {num_str} → {_format_number(-val)}",
                        )
                    )
            except ValueError:
                pass

        # 3. Ошибка ±1 в одном корне
        for i, num_str in enumerate(numbers):
            try:
                val = float(num_str)
                new_nums = list(numbers)
                new_nums[i] = _format_number(val + 1)
                wrong = separator.join(new_nums)
                candidates.append(
                    (
                        wrong,
                        "arithmetic_error_root",
                        0.75,
                        f"Арифметическая ошибка в корне: {num_str} → {_format_number(val + 1)}",
                    )
                )
            except ValueError:
                pass

        for val_str, etype, plaus, expl in candidates:
            if len(results) >= count:
                break
            norm = val_str.replace(" ", "")
            if norm not in seen:
                seen.add(norm)
                results.append(
                    Distractor(
                        value=val_str,
                        error_type=etype,
                        broken_step=0,
                        plausibility=plaus,
                        explanation=expl,
                    )
                )

        results.sort(key=lambda d: d.plausibility, reverse=True)
        return results[:count]

    # ─────────────────────────────────────────────────────────
    # Fallback: arithmetic distractors
    # ─────────────────────────────────────────────────────────

    def _arithmetic_fallback(
        self,
        correct: float,
        count: int,
        seen: set,
    ) -> List[Distractor]:
        """Последний рубеж: ±1, *2, /2, -answer."""
        results: List[Distractor] = []
        candidates = [
            (correct + 1, "off_by_one", "Вычислительная ошибка: +1"),
            (correct - 1, "off_by_one", "Вычислительная ошибка: −1"),
            (correct * 2, "magnitude", "Забыл разделить"),
            (correct / 2 if correct != 0 else 1, "magnitude", "Забыл умножить"),
            (-correct, "sign_error", "Ошибка знака"),
            (correct * 10, "magnitude_10", "Ошибка порядка: ×10"),
            (correct + 10, "off_by_ten", "Арифметическая ошибка"),
        ]

        for val, etype, explain in candidates:
            if len(results) >= count:
                break
            norm = _format_number(val)
            if norm not in seen:
                seen.add(norm)
                results.append(
                    Distractor(
                        value=norm,
                        error_type=etype,
                        broken_step=0,
                        plausibility=0.40,
                        explanation=explain,
                    )
                )

        return results


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _format_number(val: float) -> str:
    """Форматирует число: целое если возможно, иначе 4 значащие цифры."""
    if val == int(val):
        return str(int(val))
    return f"{val:.4g}"


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False
