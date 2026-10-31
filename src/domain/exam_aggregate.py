"""
Exam Service - the one aggregation of a finished exam's numbers (EX-04, EX-11, EX-22).

The "Аналитика" tab (``GET /exams/{id}/analytics``) and the "Отчёт" (``report_generator``) used to carry two copies of
this code that had drifted apart (pass threshold 50 % against 60 %, answered tasks only against all tasks).  Both now call
``aggregate_exam``; a saved report keeps the numbers it was written with and is not recomputed.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.domain.distractor_utils import resolve_analytics_explanation
from src.domain.grading import check_task_answer, regrade_if_stale, stored_verdicts
from src.domain.task_display import question_display_fields
from src.models import ExamSession, GeneratedExam

DEFAULT_PASS_THRESHOLD_PCT = 60  # the default of a new exam and the value every exam had before the threshold became its own


def exam_pass_threshold_pct(exam: Any) -> int:
    """The pass threshold (1..100, percent of the maximum score) of THIS exam; an exam row that predates the column counts 60."""
    value = getattr(exam, "pass_threshold_pct", None)
    return int(value) if value else DEFAULT_PASS_THRESHOLD_PCT


def has_passed(score: Any, max_score: Any, threshold_pct: int) -> bool:
    """``score / max_score >= threshold`` without floating-point drift (70 % of 10 points is exactly 7)."""
    if not max_score:
        return False
    return float(score or 0) * 100 >= threshold_pct * float(max_score) - 1e-9


def aggregate_exam(
    exam: GeneratedExam,
    sessions: List[ExamSession],
) -> Dict[str, Any]:
    """The numbers of one exam - ONE function for the "Аналитика" tab and for the "Отчёт" (EX-04/EX-11/EX-22).

    * the denominator of a task is every student who SUBMITTED a work with that task in his variant: the front keeps in
      ``answers`` only the tasks a student touched, so a skipped task counts as wrong (it used to vanish from the
      analytics, and a task nobody solved did not show up at all);
    * "passed" means ``score / max_score >= exam.pass_threshold_pct`` - the threshold of THIS exam, locked after the first
      submitted work (owner decision 31.10).
    """
    submitted = [s for s in sessions if s.status == "submitted"]
    n = len(submitted)
    threshold = exam_pass_threshold_pct(exam)
    # Scores and per-task correctness below must come from one grading (see grading.regrade_if_stale).
    for s in submitted:
        regrade_if_stale(s, exam)

    if n == 0:
        return {
            "total_students": len(sessions),
            "submitted_count": 0,
            "avg_score_pct": 0.0,
            "pass_rate_pct": 0.0,
            "pass_threshold_pct": threshold,
            "task_analytics": [],
            "top_errors": [],
        }

    avg_pct = sum(
        (s.score / s.max_score * 100) if s.max_score else 0
        for s in submitted
    ) / n

    pass_rate = sum(1 for s in submitted if has_passed(s.score, s.max_score, threshold)) / n * 100

    task_lookup: Dict[str, dict] = {}
    for v in (exam.variants or []):
        for t in v.get("tasks", []):
            if t["task_id"] not in task_lookup:
                task_lookup[t["task_id"]] = t

    variant_lookup: Dict[int, List[dict]] = {}
    for v in (exam.variants or []):
        v_num = v.get("variant_number", 1)
        variant_lookup[v_num] = v.get("tasks", [])

    task_correct: Dict[str, int] = {}
    task_total: Dict[str, int] = {}
    task_distractor_hits: Dict[str, Dict[str, dict]] = {}
    task_student_answers: Dict[str, List[str]] = {}
    mc_mode = (exam.settings or {}).get("mc_mode", "auto")

    for s in submitted:
        verdicts = stored_verdicts(s.distractor_analytics) or {}
        # Get all tasks that were in this student's variant
        v_tasks = variant_lookup.get(s.variant_number, [])
        if not v_tasks and exam.variants:
            v_tasks = exam.variants[0].get("tasks", [])
        
        # If variant tasks found, iterate through all of them for this student
        seen_task_ids = set()
        for t in v_tasks:
            task_id = t["task_id"]
            seen_task_ids.add(task_id)
            student_ans = (s.answers or {}).get(task_id, "")
            task_total[task_id] = task_total.get(task_id, 0) + 1
            if str(student_ans).strip():
                task_student_answers.setdefault(task_id, []).append(str(student_ans).strip())
            if student_ans and (verdicts[task_id] if task_id in verdicts else check_task_answer(t, student_ans, mc_mode)):
                task_correct[task_id] = task_correct.get(task_id, 0) + 1

        # Also account for any answered tasks outside variant list just in case
        for task_id, student_ans in (s.answers or {}).items():
            if task_id not in seen_task_ids:
                task_meta = task_lookup.get(task_id, {})
                task_total[task_id] = task_total.get(task_id, 0) + 1
                if str(student_ans).strip():
                    task_student_answers.setdefault(task_id, []).append(str(student_ans).strip())
                if student_ans and (verdicts[task_id] if task_id in verdicts else check_task_answer(task_meta, student_ans, mc_mode)):
                    task_correct[task_id] = task_correct.get(task_id, 0) + 1

        for task_id, hit in (s.distractor_analytics or {}).items():
            if not isinstance(hit, dict) or not hit.get("hit"):
                continue
            error_type = hit.get("error_type", "unknown")
            task_meta = task_lookup.get(task_id, {})
            explanation = resolve_analytics_explanation(
                hit,
                student_answer=(s.answers or {}).get(task_id, ""),
                distractor_meta=task_meta.get("distractor_meta"),
            )
            if not explanation:
                continue
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
        skill_name = task_meta.get("skill_name") or task_meta.get("toc_title") or "Задача"
        correct_answer = str(task_meta.get("correct_answer") or "").strip()

        hits = task_distractor_hits.get(task_id, {})
        distractor_hits = sorted(
            [
                {
                    "explanation": info["explanation"],
                    "error_type": info["error_type"],
                    "count": info["count"],
                    "students_pct": round(info["count"] / max(wrong, 1) * 100, 1) if wrong > 0 else 0.0,
                }
                for info in hits.values()
            ],
            key=lambda x: x["count"],
            reverse=True,
        )

        task_analytics.append({
            "task_id": task_id,
            **question_display_fields(task_meta),
            "correct_answer": correct_answer,
            "skill_name": skill_name,
            "attempts": total_attempts,
            "correct_count": correct,
            "wrong_count": wrong,
            "correct_rate": rate,
            "distractor_hits": distractor_hits,
            "answers_sample": task_student_answers.get(task_id, [])[:5],
        })

    task_analytics.sort(key=lambda x: (x["correct_rate"], -x["attempts"]))

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
        "pass_threshold_pct": threshold,
        "task_analytics": task_analytics,
        "top_errors": top_errors,
    }
