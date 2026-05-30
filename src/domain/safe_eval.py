"""
ALGO V1 — Safe Math Expression Evaluator

Безопасное вычисление математических выражений через AST-анализ.
Замена для eval() во всём проекте.

Разрешены:
- Числа (int, float)
- Арифметические операции (+, -, *, /, //, %, **)
- Унарные операции (+, -)
- Сравнения (==, !=, <, <=, >, >=)
- Логические операции (and, or, not)
- Вызовы whitelist-функций (abs, round, int, float, min, max, sqrt, pow, is_perfect_square)

Запрещено:
- Импорт, exec, eval
- Доступ к атрибутам (кроме math.*)
- Строковые операции, генераторы и т.д.
"""

import ast
import math
import operator
from typing import Any, Dict, Optional

__all__ = ["safe_eval", "safe_check_constraint"]


# Разрешённые бинарные операторы
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# Разрешённые операторы сравнения
_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

# Разрешённые унарные операторы
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}

# Разрешённые логические операторы
_BOOL_OPS = {
    ast.And: lambda vals: all(vals),
    ast.Or: lambda vals: any(vals),
}


def _is_perfect_square(n) -> bool:
    """Проверка на полный квадрат."""
    if n < 0:
        return False
    root = int(math.sqrt(n))
    return root * root == n


# Whitelist функций
_SAFE_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "int": int,
    "float": float,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "pow": pow,
    "is_perfect_square": _is_perfect_square,
}

# math.* функции
_MATH_ATTRS = {
    "sqrt",
    "floor",
    "ceil",
    "log",
    "log10",
    "sin",
    "cos",
    "tan",
    "pi",
    "e",
    "inf",
    "fabs",
    "gcd",
}


class _SafeEvaluator(ast.NodeVisitor):
    """Рекурсивный AST-evaluator без использования eval()."""

    def __init__(self, variables: Dict[str, Any]):
        self.variables = variables

    def visit(self, node: ast.AST) -> Any:
        method = f"visit_{type(node).__name__}"
        visitor = getattr(self, method, None)
        if visitor is None:
            raise ValueError(f"Запрещённая конструкция: {type(node).__name__}")
        return visitor(node)

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, (int, float, bool)):
            return node.value
        raise ValueError(f"Запрещённая константа: {type(node.value).__name__}")

    # Python < 3.8 compat (ast.Num / ast.NameConstant deprecated in 3.12)
    def visit_Num(self, node) -> Any:
        return node.n

    def visit_NameConstant(self, node) -> Any:
        return node.value

    def visit_Name(self, node: ast.Name) -> Any:
        name = node.id
        if name in self.variables:
            return self.variables[name]
        if name in _SAFE_FUNCTIONS:
            return _SAFE_FUNCTIONS[name]
        if name == "True":
            return True
        if name == "False":
            return False
        if name == "math":
            return math
        raise ValueError(f"Неизвестная переменная: {name}")

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        op_func = _BIN_OPS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Запрещённый оператор: {type(node.op).__name__}")
        left = self.visit(node.left)
        right = self.visit(node.right)
        return op_func(left, right)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        op_func = _UNARY_OPS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Запрещённый унарный оператор: {type(node.op).__name__}")
        return op_func(self.visit(node.operand))

    def visit_Compare(self, node: ast.Compare) -> Any:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            op_func = _CMP_OPS.get(type(op))
            if op_func is None:
                raise ValueError(f"Запрещённое сравнение: {type(op).__name__}")
            right = self.visit(comparator)
            if not op_func(left, right):
                return False
            left = right
        return True

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        op_func = _BOOL_OPS.get(type(node.op))
        if op_func is None:
            raise ValueError(
                f"Запрещённый логический оператор: {type(node.op).__name__}"
            )
        values = [self.visit(v) for v in node.values]
        return op_func(values)

    def visit_IfExp(self, node: ast.IfExp) -> Any:
        if self.visit(node.test):
            return self.visit(node.body)
        return self.visit(node.orelse)

    def visit_Call(self, node: ast.Call) -> Any:
        func = self.visit(node.func)
        if not callable(func):
            raise ValueError("Вызов не-функции")
        args = [self.visit(arg) for arg in node.args]
        return func(*args)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        value = self.visit(node.value)
        if value is math and node.attr in _MATH_ATTRS:
            return getattr(math, node.attr)
        raise ValueError(f"Запрещённый доступ к атрибуту: .{node.attr}")

    def visit_Tuple(self, node: ast.Tuple) -> Any:
        return tuple(self.visit(el) for el in node.elts)


def safe_eval(expression: str, variables: Optional[Dict[str, Any]] = None) -> Any:
    """
    Безопасное вычисление математического выражения.

    Args:
        expression: Строка выражения (например, "(a * h) // 2")
        variables: Словарь переменных {имя: значение}

    Returns:
        Результат вычисления

    Raises:
        ValueError: Если выражение содержит запрещённые конструкции
        SyntaxError: Если выражение синтаксически некорректно
    """
    variables = variables or {}

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise SyntaxError(f"Некорректное выражение: {expression}") from e

    evaluator = _SafeEvaluator(variables)
    return evaluator.visit(tree)


def safe_check_constraint(constraint: str, values: Dict[str, Any]) -> bool:
    """
    Безопасная проверка constraint-выражения.

    Args:
        constraint: Строка ограничения (например, "(a * h) % 2 == 0")
        values: Словарь значений переменных

    Returns:
        True если ограничение выполнено
    """
    if constraint in ("True", "true", "1", "from_preset", ""):
        return True
    return bool(safe_eval(constraint, values))
