"""
ALGO V1 — Answer Checking Utility

Единая функция проверки ответов, используемая всеми API-роутерами.
Устраняет дублирование _check_answer() в main_v2, cat_router, exam_router.
"""



def check_answer(
    student_answer: str,
    correct_answer: str,
    answer_type: str = "text",
    tolerance: float = 0.0001,
) -> bool:
    """
    Проверяет ответ ученика.

    Args:
        student_answer: Ответ ученика
        correct_answer: Правильный ответ
        answer_type: Тип ответа — exact_number, decimal, fraction, expression,
                     multiple_choice, text, ...
        tolerance: Допуск для числового сравнения

    Returns:
        True если ответ верен
    """
    # Нормализуем ответы
    student = student_answer.strip().lower().replace(",", ".").replace(" ", "")
    correct = correct_answer.strip().lower().replace(",", ".").replace(" ", "")

    if answer_type in ("exact_number", "decimal"):
        try:
            return abs(float(student) - float(correct)) < tolerance
        except ValueError:
            return student == correct

    elif answer_type == "fraction":
        try:
            from fractions import Fraction

            return Fraction(student) == Fraction(correct)
        except Exception:
            return student == correct

    elif answer_type == "expression":
        try:
            from sympy import simplify, parse_expr

            return simplify(parse_expr(student) - parse_expr(correct)) == 0
        except Exception:
            return student == correct

    elif answer_type == "multiple_choice":
        return student == correct

    else:
        # Точное сравнение
        return student == correct
