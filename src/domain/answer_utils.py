"""
Shared answer comparison utilities used by both router and report_generator.
Kept in the domain layer to avoid circular imports (domain ← api is forbidden).
"""
from __future__ import annotations


def normalise_answer(answer: str) -> str:
    return answer.strip().lower().replace(",", ".") if answer else ""


def check_answer(student: str, correct: str) -> bool:
    s, c = normalise_answer(student), normalise_answer(correct)
    if s == c:
        return True
    try:
        return abs(float(s) - float(c)) < 0.01
    except (ValueError, TypeError):
        return False
