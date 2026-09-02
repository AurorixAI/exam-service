"""
Exam Service — On-demand Report Generator (Powered by Azure DeepSeek / Gemini)

Flow:
  1. Aggregate all submitted sessions for exam with exact task analytics and distractor data.
  2. Build a comprehensive pedagogical prompt with actual problems, student answer distributions, and misconceptions.
  3. Call Azure DeepSeek (Primary) / Gemini (Fallback) in JSON mode.
  4. Upsert ExamReport row with structured JSON narrative and metrics.
"""
from __future__ import annotations

import json
import logging
import random
import string
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from src.domain.answer_utils import check_answer as _check_answer
from src.domain.distractor_utils import resolve_analytics_explanation
from src.domain.gemini_client import call_gemini, parse_json_response
from src.models import ExamSession, GeneratedExam
from src.repositories.exam_repo import ExamReportRepository

log = logging.getLogger(__name__)

_PASS_THRESHOLD = 0.6  # 60 % -> pass threshold


def _rand_id(prefix: str = "er") -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"{prefix}_{suffix}"


# ── Aggregation ───────────────────────────────────────────────────────────────

def _aggregate(
    exam: GeneratedExam,
    sessions: List[ExamSession],
) -> Dict[str, Any]:
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

    for s in submitted:
        # Get all tasks that were in this student's variant
        v_tasks = variant_lookup.get(s.variant_number, [])
        if not v_tasks and exam.variants:
            v_tasks = exam.variants[0].get("tasks", [])
        
        # If variant tasks found, iterate through all of them for this student
        seen_task_ids = set()
        for t in v_tasks:
            task_id = t["task_id"]
            seen_task_ids.add(task_id)
            correct_ans = t.get("correct_answer", "")
            student_ans = (s.answers or {}).get(task_id, "")
            task_total[task_id] = task_total.get(task_id, 0) + 1
            if str(student_ans).strip():
                task_student_answers.setdefault(task_id, []).append(str(student_ans).strip())
            if student_ans and _check_answer(student_ans, correct_ans):
                task_correct[task_id] = task_correct.get(task_id, 0) + 1

        # Also account for any answered tasks outside variant list just in case
        for task_id, student_ans in (s.answers or {}).items():
            if task_id not in seen_task_ids:
                task_meta = task_lookup.get(task_id, {})
                correct_ans = task_meta.get("correct_answer", "")
                task_total[task_id] = task_total.get(task_id, 0) + 1
                if str(student_ans).strip():
                    task_student_answers.setdefault(task_id, []).append(str(student_ans).strip())
                if student_ans and _check_answer(student_ans, correct_ans):
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
        full_q_text = (task_meta.get("question_text") or task_meta.get("question_latex") or "").strip()
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
            "question_text": full_q_text,
            "question_preview": full_q_text[:180],
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
        "task_analytics": task_analytics,
        "top_errors": top_errors,
    }


# ── Prompt Builder for DeepSeek ───────────────────────────────────────────────

def _build_deepseek_prompt(
    exam_title: str,
    grade_level: Optional[int],
    agg: Dict[str, Any],
) -> str:
    n = agg["submitted_count"]
    avg = agg["avg_score_pct"]
    pass_rate = agg["pass_rate_pct"]

    all_tasks = agg.get("task_analytics", [])

    tasks_details = []
    for idx, t in enumerate(all_tasks, 1):
        q_text = t.get("question_text") or t.get("question_preview") or t.get("skill_name") or "Задача"
        c_ans = t.get("correct_answer") or "Не указан"
        err_pct = round((1 - t["correct_rate"]) * 100)
        distractor_desc = ""
        if t.get("distractor_hits"):
            distractor_desc = "; Зафиксированные типичные когнитивные ошибки учеников: " + ", ".join(
                [f"«{d['explanation']}» ({d['count']} чел, {d['students_pct']}%)" for d in t["distractor_hits"][:3]]
            )
        tasks_details.append(
            f'Задание {idx}. Тема: «{t["skill_name"]}»\n'
            f'  Условие: «{q_text}»\n'
            f'  Правильный ответ: «{c_ans}»\n'
            f'  Статистика: успешно решили {round(t["correct_rate"] * 100)}%, ошиблись {err_pct}% ({t["wrong_count"]} из {t["attempts"]} чел.){distractor_desc}'
        )

    tasks_context = "\n\n".join(tasks_details) if tasks_details else "Нет детальных данных по заданиям."
    grade_str = f"{grade_level} класс, " if grade_level else ""

    return f"""Ты — ведущий методист-аналитик и эксперт по педагогической диагностике платформы ALGO.
Составь глубокий, математически точный и содержательный педагогический отчёт для учителя по итогам проведённой контрольной работы.

ДАННЫЕ ЭКЗАМЕНА:
- Название контрольной: «{exam_title}»
- Контингент: {grade_str}Сдано работ: {n}
- Средний балл класса: {avg}%
- Процент преодолевших порог (≥60%): {pass_rate}%

ДАННЫЕ ПО ВСЕМ ЗАДАНИЯМ, ПРАВИЛЬНЫМ ОТВЕТАМ И РАСПРЕДЕЛЕНИЮ ОШИБОК:
{tasks_context}

ПРАВИЛА АНАЛИЗА:
1. Опирайся ИСКЛЮЧИТЕЛЬНО на реальные данные заданий, приведенные выше. Никакой "воды", абстрактных фраз или шаблонных клише.
2. В анализе ошибок используй конкретные математические формулировки, правила и названия тем из заданий.
3. ВАЖНО: Если несколько заданий имеют одинаковое общее название темы (например, «Построение графика y = x²» или «Направление ветвей параболы»), формулируй названия тем в `strengths` и `critical_gaps` ДИФФЕРЕНЦИРОВАННО с указанием конкретной математической операции/сложности. Например: «Построение графика y = x² (нахождение значений и корней)» vs «Построение графика y = x² (геометрические задачи и площадь)», «Направление ветвей параболы (числовые коэффициенты)» vs «Направление ветвей параболы с параметром». Темы не должны выглядеть одинаковыми и противоречить друг другу.
4. В разборе заданий обязательно укажи реальный текст задания, правильный ответ, суть когнитивной ловушки (почему ученики выбрали неверный ответ) и детальный пошаговый совет учителю.
5. В плане урока укажи конкретные математические правила и формулы по проблемным темам этой контрольной.

Верни СТРОГО валидный JSON следующей структуры (без markdown-оберток ```json и без лишнего текста вокруг):
{{
  "summary": "2-3 развернутых содержательных предложения с конкретными цифрами (средний балл, процент сдачи, количество проблемных заданий), педагогической оценкой готовности класса к переходу к следующей теме и указанием главного фактора ошибок.",
  "strengths": [
    {{
      "topic": "Точное дифференцированное название темы/навыка (с уточнением конкретной операции в скобках)",
      "detail": "Конкретный процент успешности (X%) и детальное объяснение, какие именно математические операции ученики выполняют безошибочно."
    }}
  ],
  "critical_gaps": [
    {{
      "topic": "Точное дифференцированное название темы/пробела (с уточнением конкретной операции в скобках)",
      "detail": "Конкретный процент ошибок (Y%) и глубокое объяснение сути математического пробела (где именно сбиваются: знаки, формулы, ОДЗ, неверное сокращение)."
    }}
  ],
  "problem_tasks_analysis": [
    {{
      "task_title": "Задание N: Точное название темы",
      "question_text": "Полный текст или математическое условие задачи с формулами в LaTeX",
      "correct_answer": "Правильный ответ",
      "error_rate_pct": 50,
      "misconception": "Глубокое объяснение когнитивной ошибки: какую ловушку допустили ученики, на что повелись, какое неверное правило применили.",
      "teacher_advice": "Развернутый пошаговый методический совет учителю: как именно разобрать этот шаг у доски, на какую формулу/правило обратить внимание и как провести самопроверку."
    }}
  ],
  "lesson_plan": {{
    "warmup": {{
      "duration": "5–7 мин",
      "focus": "Конкретная цель устной разминки по проблемным темам работы",
      "key_rules": ["Конкретное математическое правило или формула 1 по темам работы", "Конкретное математическое правило или формула 2"]
    }},
    "board_tasks": [
      {{
        "title": "Разбор задания по теме «...»",
        "focus": "На какой именно шаг решения и математическую ловушку обратить внимание",
        "steps": ["Шаг 1 разбора с обоснованием", "Шаг 2 с акцентом на ликвидацию типичной ошибки", "Шаг 3 проверка подстановкой и запись ответа"]
      }}
    ],
    "practice": {{
      "duration": "10–15 мин",
      "task_recommendation": "Конкретная рекомендация по организации практикума с типами тренировочных заданий по выявленным пробелам."
    }},
    "homework": {{
      "struggling_group": "Конкретные задания и темы для группы риска (отработка базового алгоритма без усложнений).",
      "advanced_group": "Задания повышенной сложности и нестандартные задачи для сильных учеников."
    }}
  }}
}}
"""


# ── Pedagogical Rules Helper for Deterministic Fallback ───────────────────────

def _extract_topic_rules(skill_names: List[str]) -> List[str]:
    """Generates context-aware math rules based on actual skills present in problem tasks."""
    rules = []
    text = " ".join(skill_names).lower()

    if any(w in text for w in ["парабол", "квадратичн", "вершин", "ось"]):
        rules.append("Квадратичная функция $y = ax^2 + bx + c$: при $a > 0$ ветви направлены вверх, при $a < 0$ — вниз")
        rules.append("Ось симметрии параболы: $x_0 = -\\frac{b}{2a}$, вершина параболы лежит на оси симметрии")

    if any(w in text for w in ["дроб", "сокращ", "рациональн", "знаменател"]):
        rules.append("Основное свойство дроби: сокращать можно только общие множители, почленное сокращение слагаемых недопустимо")
        rules.append("Смена знака в разности: $(a - b) = -(b - a)$, но $(a - b)^2 = (b - a)^2$")

    if any(w in text for w in ["корен", "уравнен", "линейн", "параметр"]):
        rules.append("При переносе слагаемых через знак равенства знак меняется на противоположный")
        rules.append("Обязательная проверка корней подстановкой в исходное уравнение")

    if any(w in text for w in ["степен", "показател", "умножен"]):
        rules.append("Свойства степеней: $a^m \\cdot a^n = a^{m+n}$, $(a^m)^n = a^{m \\cdot n}$, $(ab)^n = a^n b^n$")

    if not rules:
        rules.append("Внимательный контроль знаков при раскрытии скобок и переносе слагаемых")
        rules.append("Проверка полученного ответа подстановкой в условие задачи")

    return rules[:3]


# ── Deterministic Fallback (Deep Pedagogical & Data-Driven) ───────────────────

def _build_deterministic_fallback(
    exam_title: str,
    grade_level: Optional[int],
    agg: Dict[str, Any],
) -> Dict[str, Any]:
    n = agg["submitted_count"]
    avg = agg["avg_score_pct"]
    pass_rate = agg["pass_rate_pct"]
    all_tasks = agg.get("task_analytics", [])
    grade_str = f" в {grade_level} классе" if grade_level else ""

    strong_tasks = [t for t in all_tasks if t["correct_rate"] >= 0.65 and t["attempts"] > 0]
    problem_tasks = [t for t in all_tasks if t["correct_rate"] < 0.65 and t["attempts"] > 0]
    problem_tasks.sort(key=lambda x: x["correct_rate"])

    # 1. Summary
    if avg >= 75:
        verdict = "Класс продемонстрировал высокий уровень освоения программного материала и готов к переходу к следующему разделу."
    elif avg >= 55:
        verdict = "Класс показал удовлетворительное владение базовыми алгоритмами, однако требует точечной коррекции типичных вычислительных и логических ошибок."
    else:
        verdict = "Результаты указывают на наличие системных пробелов в ключевых темах раздела; классу необходим полноценный урок работы над ошибками."

    summary_text = (
        f"По итогам контрольной работы «{exam_title}»{grade_str} средний результат класса составил {avg}%, "
        f"порог успешности (≥60%) преодолели {pass_rate}% сдавших учащихся ({n} чел.). "
        f"Из {len(all_tasks)} заданий работы успешно освоены {len(strong_tasks)}, в то время как {len(problem_tasks)} заданий "
        f"выявили затруднения. {verdict}"
    )

    # 2. Strengths
    strengths = []
    for t in strong_tasks[:4]:
        pct = round(t["correct_rate"] * 100)
        skill = t.get("skill_name") or "Базовые вычисления"
        strengths.append({
            "topic": skill,
            "detail": (
                f"{pct}% учащихся ({t['correct_count']} из {t['attempts']} чел.) безошибочно решили задание по этой теме. "
                f"Ученики уверенно применяют базовый алгоритм и не допускают вычислительных сбоев."
            ),
        })
    if not strengths:
        strengths.append({
            "topic": "Базовые математические операции",
            "detail": "Учащиеся продемонстрировали понимание базовых определений и формата оформления заданий контрольной работы.",
        })

    # 3. Critical Gaps
    gaps = []
    for t in problem_tasks[:4]:
        err_pct = round((1 - t["correct_rate"]) * 100)
        skill = t.get("skill_name") or "Проблемный раздел"
        
        # Extract concrete distractor logic if available
        if t.get("distractor_hits"):
            top_hit = t["distractor_hits"][0]
            misc_desc = f"Основная причина ({top_hit.get('students_pct', err_pct)}% ошибок): {top_hit['explanation']}"
        else:
            misc_desc = "Затруднения при выполнении многошаговых преобразований и контроле знаков/коэффициентов"

        gaps.append({
            "topic": skill,
            "detail": f"{err_pct}% учащихся ({t['wrong_count']} из {t['attempts']} чел.) допустили ошибки. {misc_desc}.",
        })
    if not gaps:
        gaps.append({
            "topic": "Закрепление материала повышенной сложности",
            "detail": "Критических системных пробелов не зафиксировано; рекомендуется перейти к углубленным комбинированным задачам.",
        })

    # 4. Problem Tasks Analysis
    problem_analysis = []
    tasks_to_analyze = problem_tasks[:6] if problem_tasks else all_tasks[:3]
    for idx, t in enumerate(tasks_to_analyze, 1):
        err_pct = round((1 - t["correct_rate"]) * 100)
        skill = t.get("skill_name") or "Задача"
        q_text = t.get("question_text") or t.get("question_preview") or ""
        correct_ans = t.get("correct_answer") or ""

        # Rich misconception description based on actual distractors
        if t.get("distractor_hits"):
            hits_descs = []
            for h in t["distractor_hits"][:2]:
                hits_descs.append(f"• {h['explanation']} ({h['count']} чел., {h['students_pct']}%)")
            misconception = "Зафиксированы следующие когнитивные ловушки:\n" + "\n".join(hits_descs)
        else:
            misconception = (
                f"Учащиеся испытали затруднения в теме «{skill}» при анализе условий задачи и выполнении преобразований. "
                f"Типичный сбой связан с неверным выбором промежуточного шага или потерей коэффициента."
            )

        # Topic-aware teacher advice
        teacher_advice = (
            f"1. Разобрать на доске исходное задание по теме «{skill}», четко зафиксировав ключевые свойства и формулы.\n"
            f"2. Показать типичную ловушку и объяснить, почему промежуточный шаг приводит к ошибочному ответу.\n"
            f"3. Провести быструю самопроверку полученного результата через обратную подстановку."
        )

        problem_analysis.append({
            "task_title": f"Задание {idx}: {skill}",
            "question_text": q_text,
            "correct_answer": correct_ans,
            "error_rate_pct": err_pct,
            "misconception": misconception,
            "teacher_advice": teacher_advice,
        })

    # 5. Lesson Plan
    problem_skills = [t.get("skill_name") or "" for t in problem_tasks if t.get("skill_name")]
    key_rules = _extract_topic_rules(problem_skills)
    top_topics_str = ", ".join(problem_skills[:2]) if problem_skills else "пройденным темам"

    board_tasks_plan = []
    for t in problem_tasks[:2]:
        s_name = t.get("skill_name") or "Задание"
        top_err = t["distractor_hits"][0]["explanation"] if t.get("distractor_hits") else f"ошибки в теме {s_name}"
        board_tasks_plan.append({
            "title": f"Разбор задания: «{s_name}»",
            "focus": f"Устранение ловушки: {top_err[:80]}",
            "steps": [
                "Выписать условие, выделить исходные коэффициенты и ограничения задачи",
                "Выполнить преобразования с явной промежуточной записью скобок и знаков",
                "Выполнить проверку подстановкой и записать итоговый ответ",
            ],
        })

    if not board_tasks_plan and all_tasks:
        first_task = all_tasks[0]
        board_tasks_plan.append({
            "title": f"Разбор типовой задачи: «{first_task.get('skill_name') or 'Контрольное задание'}»",
            "focus": "Пошаговая демонстрация эталонного хода решения",
            "steps": [
                "Анализ условия и выделение ключевых формул",
                "Пошаговые преобразования с проверкой каждого действия",
                "Запись и обоснование окончательного ответа",
            ],
        })

    return {
        "summary": summary_text,
        "strengths": strengths,
        "critical_gaps": gaps,
        "problem_tasks_analysis": problem_analysis,
        "lesson_plan": {
            "warmup": {
                "duration": "5–7 мин",
                "focus": f"Устная актуализация опорных понятий и формул по темам: {top_topics_str}",
                "key_rules": key_rules,
            },
            "board_tasks": board_tasks_plan,
            "practice": {
                "duration": "10–15 мин",
                "task_recommendation": f"Решение 2–3 аналогичных тренировочных задач в парах по теме «{top_topics_str}» с взаимопроверкой по эталону.",
            },
            "homework": {
                "struggling_group": f"Обязательная работа над ошибками своего варианта + 4 базовых примера на закрепление по теме «{top_topics_str}».",
                "advanced_group": f"2 комбинированных задания повышенной сложности по теме «{top_topics_str}» с параметром или исследованием свойств.",
            },
        },
    }


# ── Main Entry Point ──────────────────────────────────────────────────────────

def generate_exam_report(
    db: Session,
    *,
    exam: GeneratedExam,
    sessions: List[ExamSession],
    force: bool = False,
) -> Optional[Any]:
    """
    Aggregate sessions, call Azure DeepSeek (or Gemini fallback), upsert ExamReport.
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

    # AI narrative JSON generation
    report_dict: Optional[Dict[str, Any]] = None
    try:
        submitted = [s for s in sessions if s.status == "submitted"]
        grade = next((s.class_level for s in submitted if s.class_level), None)
        prompt = _build_deepseek_prompt(exam.title, grade, agg)

        raw_response = call_gemini(
            prompt,
            temperature=0.3,
            max_tokens=8000,
            json_mode=True,
        )
        report_dict = parse_json_response(raw_response)
        if not report_dict or not report_dict.get("summary"):
            log.warning("DeepSeek output invalid JSON; using deterministic fallback")
            report_dict = _build_deterministic_fallback(exam.title, grade, agg)
        else:
            log.info("DeepSeek report successfully generated for exam %s", exam.id)
    except Exception as exc:
        log.warning("AI generation failed for exam %s: %s — using deterministic fallback", exam.id, exc)
        submitted = [s for s in sessions if s.status == "submitted"]
        grade = next((s.class_level for s in submitted if s.class_level), None)
        report_dict = _build_deterministic_fallback(exam.title, grade, agg)

    narrative_json_str = json.dumps(report_dict, ensure_ascii=False)

    try:
        is_final = exam.status == "closed" or exam.status == "archived"
        report = repo.upsert(
            exam.id,
            _rand_id("er"),
            total_students=agg["total_students"],
            submitted_count=agg["submitted_count"],
            avg_score_pct=agg["avg_score_pct"],
            pass_rate_pct=agg["pass_rate_pct"],
            task_analytics=agg["task_analytics"],
            top_errors=agg["top_errors"],
            narrative_ru=narrative_json_str,
            narrative_generated=True,
            is_final=is_final,
        )
        log.info(
            "ExamReport saved for exam %s (submitted=%d, narrative=yes, final=%s)",
            exam.id, agg["submitted_count"], is_final,
        )
        return report
    except Exception as exc:
        log.error("Failed to save ExamReport for exam %s: %s", exam.id, exc, exc_info=True)
        db.rollback()
        return None
