"""
Exam Service — On-demand Report Generator

Flow:
  1. Aggregate all submitted sessions for exam  (same logic as /analytics)
  2. Build a concise prompt from the aggregated data
  3. Call Gemini Flash → narrative_ru
  4. Upsert ExamReport row (idempotent)

Called only by POST /exams/{exam_id}/report.
Never raises — errors are logged, None is returned.
"""
from __future__ import annotations

import logging
import random
import string
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from src.domain.answer_utils import check_answer as _check_answer
from src.domain.gemini_client import call_gemini
from src.models import ExamSession, GeneratedExam
from src.repositories.exam_repo import ExamReportRepository

log = logging.getLogger(__name__)

# ── Level labels ───────────────────────────────────────────────────────────────

_PASS_THRESHOLD = 0.6  # 60 % → зачёт


def _rand_id(prefix: str = "er") -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"{prefix}_{suffix}"


# ── Aggregation (mirrors get_exam_analytics logic) ────────────────────────────

def _aggregate(
    exam: GeneratedExam,
    sessions: List[ExamSession],
) -> Dict[str, Any]:
    """Return the same shape as ExamAnalyticsResponse, ready for the prompt."""
    submitted = [s for s in sessions if s.status == "submitted"]
    n = len(submitted)

    if n == 0:
        return {
            "total_students": len(sessions),
            "submitted_count": 0,
            "avg_score_pct": 0.0,
            "pass_rate_pct": 0.0,
            "task_analytics": [],
            "top_errors": [],
        }

    avg_pct = sum(
        (s.score / s.max_score * 100) if s.max_score else 0
        for s in submitted
    ) / n

    pass_rate = sum(
        1 for s in submitted
        if s.max_score and (s.score / s.max_score) >= _PASS_THRESHOLD
    ) / n * 100

    # Task metadata lookup
    task_lookup: Dict[str, dict] = {}
    for v in (exam.variants or []):
        for t in v.get("tasks", []):
            if t["task_id"] not in task_lookup:
                task_lookup[t["task_id"]] = t

    task_correct: Dict[str, int] = {}
    task_total: Dict[str, int] = {}
    task_distractor_hits: Dict[str, Dict[str, dict]] = {}

    for s in submitted:
        for task_id, student_ans in (s.answers or {}).items():
            task_meta = task_lookup.get(task_id, {})
            correct_ans = task_meta.get("correct_answer", "")
            task_total[task_id] = task_total.get(task_id, 0) + 1
            if _check_answer(student_ans, correct_ans):
                task_correct[task_id] = task_correct.get(task_id, 0) + 1

        for task_id, hit in (s.distractor_analytics or {}).items():
            if not isinstance(hit, dict) or not hit.get("hit"):
                continue
            error_type = hit.get("error_type", "unknown")
            explanation = hit.get("explanation", "")
            key = f"{error_type}::{explanation[:60]}"
            bucket = task_distractor_hits.setdefault(task_id, {})
            if key not in bucket:
                bucket[key] = {"explanation": explanation, "error_type": error_type, "count": 0}
            bucket[key]["count"] += 1

    all_task_ids = set(task_total.keys()) | set(task_distractor_hits.keys())
    task_analytics = []
    for task_id in all_task_ids:
        total_attempts = task_total.get(task_id, 0)
        correct = task_correct.get(task_id, 0)
        wrong = total_attempts - correct
        rate = round(correct / total_attempts, 3) if total_attempts else 0.0

        task_meta = task_lookup.get(task_id, {})
        skill_name = task_meta.get("skill_name") or task_meta.get("toc_title") or ""
        question_preview = (task_meta.get("question_text") or "")[:120].strip()

        hits = task_distractor_hits.get(task_id, {})
        distractor_hits = sorted(
            [
                {
                    "explanation": info["explanation"],
                    "count": info["count"],
                    "students_pct": round(info["count"] / max(wrong, 1) * 100, 1),
                }
                for info in hits.values()
            ],
            key=lambda x: x["count"],
            reverse=True,
        )

        task_analytics.append({
            "question_preview": question_preview,
            "skill_name": skill_name,
            "attempts": total_attempts,
            "correct_count": correct,
            "wrong_count": wrong,
            "correct_rate": rate,
            "distractor_hits": distractor_hits,
        })

    task_analytics.sort(key=lambda x: (x["correct_rate"], -x["attempts"]))

    # Top-5 errors
    error_aggregate: Dict[str, dict] = {}
    for item in task_analytics:
        for hit in item["distractor_hits"]:
            key = hit["explanation"][:80]
            if key not in error_aggregate:
                error_aggregate[key] = {
                    "explanation": hit["explanation"],
                    "total_count": 0,
                    "affected_tasks": [],
                }
            error_aggregate[key]["total_count"] += hit["count"]
            error_aggregate[key]["affected_tasks"].append(item.get("skill_name") or "")

    top_errors = sorted(
        [
            {
                "explanation": v["explanation"],
                "total_count": v["total_count"],
                "affected_skills": list(dict.fromkeys(v["affected_tasks"]))[:3],
                "students_pct": round(v["total_count"] / max(n, 1) * 100, 1),
            }
            for v in error_aggregate.values()
        ],
        key=lambda x: x["total_count"],
        reverse=True,
    )[:5]

    return {
        "total_students": len(sessions),
        "submitted_count": n,
        "avg_score_pct": round(avg_pct, 1),
        "pass_rate_pct": round(pass_rate, 1),
        "task_analytics": task_analytics,
        "top_errors": top_errors,
    }


# ── Prompt builder ─────────────────────────────────────────────────────────────

def _build_prompt(
    exam_title: str,
    grade_level: Optional[int],
    agg: Dict[str, Any],
) -> str:
    n = agg["submitted_count"]
    avg = agg["avg_score_pct"]
    pass_rate = agg["pass_rate_pct"]

    # Problem tasks (correct_rate < 0.5)
    problem_tasks = [
        t for t in agg["task_analytics"]
        if t["correct_rate"] < 0.5 and t["attempts"] > 0
    ]
    problem_lines = []
    for t in problem_tasks[:5]:
        preview = t["question_preview"] or t["skill_name"] or "Задача"
        pct_wrong = round((1 - t["correct_rate"]) * 100)
        problem_lines.append(f'  • "{preview[:80]}" — {pct_wrong}% не справились')

    top_errors = agg["top_errors"]
    error_lines = []
    for e in top_errors[:4]:
        affected = ", ".join(e["affected_skills"][:2]) if e["affected_skills"] else ""
        error_lines.append(
            f'  • {e["explanation"]} '
            f'({e["total_count"]} уч., {e["students_pct"]}%)'
            + (f' — темы: {affected}' if affected else '')
        )

    problem_block = "\n".join(problem_lines) if problem_lines else "  Проблемных задач не выявлено"
    error_block = "\n".join(error_lines) if error_lines else "  Системных ошибок не выявлено"
    grade_str = f"{grade_level} класс, " if grade_level else ""

    return f"""Ты — педагогический аналитик платформы ALGO. Напиши профессиональный отчёт для учителя по итогам контрольной работы.

ДАННЫЕ КОНТРОЛЬНОЙ:
- Название: «{exam_title}»
- {grade_str}сдало учеников: {n}
- Средний балл: {avg}%
- Процент сдавших (≥60%): {pass_rate}%

ПРОБЛЕМНЫЕ ЗАДАЧИ (менее 50% справились):
{problem_block}

ТОП СИСТЕМНЫХ ОШИБОК:
{error_block}

ТРЕБОВАНИЯ К ОТЧЁТУ:
- Обратись к учителю напрямую
- 3–4 абзаца: (1) общий итог класса, (2) сильные стороны, (3) конкретные проблемные темы и типичные ошибки, (4) рекомендация — что разобрать на следующем уроке
- Деловой, конкретный тон; называй темы и задачи по имени
- Не упоминай технические термины (task_id, correct_rate, дистрактор)
- Если нет проблем — акцент на закреплении успеха
- Только текст, без markdown-заголовков и списков

Напиши отчёт на русском языке:"""


# ── Main entry point ───────────────────────────────────────────────────────────

def generate_exam_report(
    db: Session,
    *,
    exam: GeneratedExam,
    sessions: List[ExamSession],
    force: bool = False,
) -> Optional[Any]:
    """
    Aggregate sessions, call Gemini, upsert ExamReport.
    Returns ExamReport on success, None on failure.
    """
    repo = ExamReportRepository(db)

    if not force:
        existing = repo.get_by_exam(exam.id)
        if existing:
            log.info("ExamReport already exists for exam %s — returning cached", exam.id)
            return existing

    agg = _aggregate(exam, sessions)

    if agg["submitted_count"] == 0:
        log.warning("No submitted sessions for exam %s — skipping report", exam.id)
        return None

    # Gemini narrative
    narrative: Optional[str] = None
    try:
        # Grade from first submitted session class_level
        submitted = [s for s in sessions if s.status == "submitted"]
        grade = next((s.class_level for s in submitted if s.class_level), None)
        prompt = _build_prompt(exam.title, grade, agg)
        narrative = call_gemini(
            prompt,
            temperature=0.45,
            max_tokens=900,
            json_mode=False,
        )
        log.info("Gemini narrative generated for exam %s (%d chars)", exam.id, len(narrative))
    except Exception as exc:
        log.warning("Narrative generation failed for exam %s: %s — saving without narrative", exam.id, exc)

    try:
        is_final = exam.status == "closed"
        report = repo.upsert(
            exam.id,
            _rand_id("er"),
            total_students=agg["total_students"],
            submitted_count=agg["submitted_count"],
            avg_score_pct=agg["avg_score_pct"],
            pass_rate_pct=agg["pass_rate_pct"],
            task_analytics=agg["task_analytics"],
            top_errors=agg["top_errors"],
            narrative_ru=narrative,
            narrative_generated=narrative is not None,
            is_final=is_final,
        )
        log.info(
            "ExamReport saved for exam %s (submitted=%d, narrative=%s, final=%s)",
            exam.id, agg["submitted_count"], "yes" if narrative else "no", is_final,
        )
        return report

    except Exception as exc:
        log.error("Failed to save ExamReport for exam %s: %s", exam.id, exc)
        db.rollback()
        return None
