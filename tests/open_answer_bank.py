"""Regression of the typed-answer grader against task banks (used by the test-suite and by the CLI script).

For every task:
  * the key typed without any ``$`` must be accepted;
  * no stored distractor of the same task, typed as stored or without ``$``, may be accepted as the key.
``BANK_DEFECTS`` are tasks whose distractor genuinely IS the key (checked by hand, see the notes); they are reported
for the content team and are the only tolerated violations.
"""
from __future__ import annotations

from typing import Iterable

from src.domain.answer_utils import check_answer, check_open_answer

BANK_DEFECTS = {
    # distractor `+∞` is the key `+\infty` written in Unicode
    "G9_TB_ЗПТ_835_1": "distractor equals key (+∞ = +\\infty)",
    # distractor `172.5` is the key `172\dfrac{1}{2}` as a decimal
    "G9_TB_ТЗГ4_11": "distractor equals key (172.5 = 172 1/2)",
}


def distractor_values(row: dict) -> list[str]:
    values = []
    for item in row.get("distractors") if "distractors" in row else (row.get("distractor_meta") or []):
        value = item if isinstance(item, str) else str((item or {}).get("value") or (item or {}).get("text") or (item or {}).get("content") or "")
        if value.strip():
            values.append(value)
    return values


def regression(rows: Iterable[dict]) -> dict:
    stats = {
        "tasks": 0,
        "keys_with_dollar": 0,
        "self_typed_ok_legacy": 0,
        "self_typed_ok_open": 0,
        "tasks_with_distractor_accepted_legacy": 0,
        "tasks_with_distractor_accepted_open": 0,
        "self_failures": [],
        "violations": [],
    }
    for row in rows:
        key = str(row["correct_answer"])
        typed = key.replace("$", "").strip()
        distractors = distractor_values(row)
        stats["tasks"] += 1
        stats["keys_with_dollar"] += "$" in key
        stats["self_typed_ok_legacy"] += check_answer(typed, key)
        if check_open_answer(typed, key):
            stats["self_typed_ok_open"] += 1
        else:
            stats["self_failures"].append((row["id"], key))
        if any(check_answer(d.replace("$", "").strip(), key) or check_answer(d, key) for d in distractors):
            stats["tasks_with_distractor_accepted_legacy"] += 1
        bad = [d for d in distractors if check_open_answer(d.replace("$", "").strip(), key) or check_open_answer(d, key)]
        if bad:
            stats["tasks_with_distractor_accepted_open"] += 1
            stats["violations"].append((row["id"], key, bad))
    return stats
