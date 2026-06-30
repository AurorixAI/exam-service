"""
ALGO V1 — Exam Analytics
src/exam_builder/analytics.py

Пост-экзаменная аналитика с разделением на два уровня:

• Задачи с маппингом на ядро (skill_id IS NOT NULL):
  – Полная диагностика: дистрактор-анализ, определение пробелов,
    рекомендации по prerequisite-цепочке, deep scan
• Задачи без маппинга (textbook-only, skill_id IS NULL):
  – Только верно/неверно + правильный ответ

Используется ПОСЛЕ сдачи экзамена, когда собраны ответы ученика.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

from src.domain.answer_check import check_answer as _shared_check_answer
from src.domain.distractor_utils import distractor_text

log = logging.getLogger("exam_analytics")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class StudentAnswer:
    """Ответ ученика на одну задачу экзамена."""

    task_id: str
    student_answer: str
    time_seconds: float = 0.0


@dataclass
class BasicTaskResult:
    """Базовый результат для textbook-only задачи (без маппинга на ядро)."""

    task_id: str
    correct: bool
    correct_answer: str
    student_answer: str
    time_seconds: float = 0.0


@dataclass
class DistractorInsight:
    """Анализ одного дистрактора: почему ученик мог выбрать этот ответ."""

    distractor_value: str
    error_type: str  # sign_error, off_by_one, transfer_error, …
    explanation: str  # Человекочитаемое объяснение
    matches_student: bool  # Совпадает ли с ответом ученика


@dataclass
class SkillGap:
    """Обнаруженный пробел в навыке."""

    skill_id: str
    skill_name: str
    gap_type: str  # missing_prerequisite, weak_mastery, procedural_error
    severity: float  # 0.0 — 1.0
    recommendation: str  # Что нужно повторить


@dataclass
class MappedTaskResult:
    """Полный результат для задачи с маппингом на ядро."""

    task_id: str
    skill_id: str
    skill_name: str
    correct: bool
    correct_answer: str
    student_answer: str
    time_seconds: float = 0.0

    # Полная диагностика
    distractor_insights: List[DistractorInsight] = field(default_factory=list)
    skill_gaps: List[SkillGap] = field(default_factory=list)
    error_pattern: str = ""  # Описание паттерна ошибки
    prerequisite_chain: List[str] = field(
        default_factory=list
    )  # skill_ids от пробела до цели


@dataclass
class ExamAnalyticsReport:
    """Итоговый отчёт аналитики по экзамену."""

    exam_id: str
    student_id: str
    total_tasks: int
    total_correct: int
    score_percent: float

    # Задачи с маппингом на ядро — полная диагностика
    mapped_results: List[MappedTaskResult] = field(default_factory=list)

    # Задачи без маппинга — только верно/неверно
    basic_results: List[BasicTaskResult] = field(default_factory=list)

    # Агрегированные пробелы (из mapped_results)
    skill_gaps_summary: List[SkillGap] = field(default_factory=list)

    # Статистика
    mapped_count: int = 0
    mapped_correct: int = 0
    basic_count: int = 0
    basic_correct: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Сериализация для API."""
        return {
            "exam_id": self.exam_id,
            "student_id": self.student_id,
            "total_tasks": self.total_tasks,
            "total_correct": self.total_correct,
            "score_percent": round(self.score_percent, 1),
            "skill_analytics": {
                "count": self.mapped_count,
                "correct": self.mapped_correct,
                "tasks": [
                    {
                        "task_id": r.task_id,
                        "skill_id": r.skill_id,
                        "skill_name": r.skill_name,
                        "correct": r.correct,
                        "student_answer": r.student_answer,
                        "correct_answer": r.correct_answer,
                        "error_pattern": r.error_pattern,
                        "distractor_insights": [
                            {
                                "value": d.distractor_value,
                                "error_type": d.error_type,
                                "explanation": d.explanation,
                                "matches_student": d.matches_student,
                            }
                            for d in r.distractor_insights
                        ],
                        "skill_gaps": [
                            {
                                "skill_id": g.skill_id,
                                "skill_name": g.skill_name,
                                "gap_type": g.gap_type,
                                "severity": g.severity,
                                "recommendation": g.recommendation,
                            }
                            for g in r.skill_gaps
                        ],
                    }
                    for r in self.mapped_results
                ],
            },
            "textbook_results": {
                "count": self.basic_count,
                "correct": self.basic_correct,
                "tasks": [
                    {
                        "task_id": r.task_id,
                        "correct": r.correct,
                        "student_answer": r.student_answer,
                        "correct_answer": r.correct_answer,
                    }
                    for r in self.basic_results
                ],
            },
            "skill_gaps_summary": [
                {
                    "skill_id": g.skill_id,
                    "skill_name": g.skill_name,
                    "gap_type": g.gap_type,
                    "severity": g.severity,
                    "recommendation": g.recommendation,
                }
                for g in self.skill_gaps_summary
            ],
        }


# ---------------------------------------------------------------------------
# Main analytics engine
# ---------------------------------------------------------------------------


class ExamAnalytics:
    """
    Пост-экзаменная аналитика.

    Разделяет задачи на два потока:
      • mapped  (skill_id != NULL) → полный анализ
      • basic   (skill_id  = NULL) → correct / incorrect
    """

    def __init__(self, db_session=None):
        self.db = db_session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(
        self,
        exam_id: str,
        student_id: str,
        answers: List[StudentAnswer],
        tasks_meta: List[Dict[str, Any]],
    ) -> ExamAnalyticsReport:
        """
        Анализирует ответы ученика.

        Args:
            exam_id: UUID экзамена
            student_id: UUID ученика
            answers: список ответов (task_id + student_answer + time)
            tasks_meta: метаданные задач из экзамена — list of dicts:
                {
                    "task_id": str,
                    "skill_id": str | None,
                    "skill_name": str,
                    "correct_answer": str,
                    "distractors": [{"value":..., "error_type":..., "explanation":...}],
                }

        Returns:
            ExamAnalyticsReport
        """
        meta_by_id: Dict[str, Dict[str, Any]] = {m["task_id"]: m for m in tasks_meta}
        {a.task_id: a for a in answers}

        mapped_results: List[MappedTaskResult] = []
        basic_results: List[BasicTaskResult] = []

        for ans in answers:
            meta = meta_by_id.get(ans.task_id, {})
            correct_answer = meta.get("correct_answer", "")
            answer_type = meta.get("answer_type", "text")
            is_correct = self._check_answer(
                ans.student_answer, correct_answer, answer_type
            )
            skill_id = meta.get("skill_id")

            if skill_id:
                # ── Полная аналитика ──
                result = self._analyze_mapped_task(
                    ans, meta, is_correct, correct_answer
                )
                mapped_results.append(result)
            else:
                # ── Только верно/неверно ──
                basic_results.append(
                    BasicTaskResult(
                        task_id=ans.task_id,
                        correct=is_correct,
                        correct_answer=correct_answer,
                        student_answer=ans.student_answer,
                        time_seconds=ans.time_seconds,
                    )
                )

        # Агрегируем пробелы из mapped
        all_gaps = []
        for r in mapped_results:
            all_gaps.extend(r.skill_gaps)
        unique_gaps = self._deduplicate_gaps(all_gaps)

        total_correct = sum(1 for r in mapped_results if r.correct) + sum(
            1 for r in basic_results if r.correct
        )
        total_tasks = len(answers)

        return ExamAnalyticsReport(
            exam_id=exam_id,
            student_id=student_id,
            total_tasks=total_tasks,
            total_correct=total_correct,
            score_percent=(total_correct / total_tasks * 100) if total_tasks else 0,
            mapped_results=mapped_results,
            basic_results=basic_results,
            skill_gaps_summary=unique_gaps,
            mapped_count=len(mapped_results),
            mapped_correct=sum(1 for r in mapped_results if r.correct),
            basic_count=len(basic_results),
            basic_correct=sum(1 for r in basic_results if r.correct),
        )

    # ------------------------------------------------------------------
    # Mapped task analysis
    # ------------------------------------------------------------------

    def _analyze_mapped_task(
        self,
        answer: StudentAnswer,
        meta: Dict[str, Any],
        is_correct: bool,
        correct_answer: str,
    ) -> MappedTaskResult:
        """Полный анализ задачи с маппингом на ядро."""

        skill_id = meta["skill_id"]
        skill_name = meta.get("skill_name", skill_id)

        # 1. Дистрактор-анализ
        distractor_insights = self._analyze_distractors(
            answer.student_answer, meta.get("distractors", [])
        )

        # 2. Определение паттерна ошибки
        error_pattern = ""
        if not is_correct:
            matching = [d for d in distractor_insights if d.matches_student]
            if matching:
                error_pattern = matching[0].error_type
            else:
                error_pattern = "unknown_error"

        # 3. Пробелы в навыках (distractor error_type + prerequisite chain)
        skill_gaps: List[SkillGap] = []
        if not is_correct:
            skill_gaps = self._identify_skill_gaps(
                skill_id, skill_name, error_pattern, meta
            )

        # 4. Prerequisite chain
        prerequisite_chain = self._get_prerequisite_chain(skill_id)

        return MappedTaskResult(
            task_id=answer.task_id,
            skill_id=skill_id,
            skill_name=skill_name,
            correct=is_correct,
            correct_answer=correct_answer,
            student_answer=answer.student_answer,
            time_seconds=answer.time_seconds,
            distractor_insights=distractor_insights,
            skill_gaps=skill_gaps,
            error_pattern=error_pattern,
            prerequisite_chain=prerequisite_chain,
        )

    # ------------------------------------------------------------------
    # Distractor analysis
    # ------------------------------------------------------------------

    def _analyze_distractors(
        self,
        student_answer: str,
        distractors: List[Dict[str, Any]],
    ) -> List[DistractorInsight]:
        """Сопоставляет ответ ученика с дистракторами."""
        insights = []
        ans_norm = self._normalize_answer(student_answer)

        for d in distractors:
            d_norm = self._normalize_answer(str(d.get("value", "")))
            insights.append(
                DistractorInsight(
                    distractor_value=str(d.get("value", "")),
                    error_type=d.get("error_type", "unknown"),
                    explanation=distractor_text(d),
                    matches_student=(ans_norm == d_norm),
                )
            )
        return insights

    # ------------------------------------------------------------------
    # Skill gap detection
    # ------------------------------------------------------------------

    def _identify_skill_gaps(
        self,
        skill_id: str,
        skill_name: str,
        error_pattern: str,
        meta: Dict[str, Any],
    ) -> List[SkillGap]:
        """Определяет пробелы по навыку на основе error_pattern и prerequisites."""
        gaps: List[SkillGap] = []

        # Если нет конкретных ошибок — общий пробел по данному навыку
        if not gaps:
            gaps.append(
                SkillGap(
                    skill_id=skill_id,
                    skill_name=skill_name,
                    gap_type="weak_mastery",
                    severity=0.6,
                    recommendation=f"Повторить: {skill_name}",
                )
            )

        return gaps

    def _get_prerequisite_chain(self, skill_id: str) -> List[str]:
        """
        Получает цепочку prerequisites для навыка из БД.

        Возвращает список skill_ids от самого базового до целевого.
        """
        if not self.db:
            return [skill_id]

        try:
            from sqlalchemy import text

            result = self.db.execute(
                text("""
                    WITH RECURSIVE chain AS (
                        SELECT prerequisite_id, 1 AS depth
                        FROM skill_prerequisites
                        WHERE skill_id = :sid AND dependency_type = 'hard'
                        UNION ALL
                        SELECT sp.prerequisite_id, chain.depth + 1
                        FROM skill_prerequisites sp
                        JOIN chain ON sp.skill_id = chain.prerequisite_id
                        WHERE sp.dependency_type = 'hard' AND chain.depth < 5
                    )
                    SELECT prerequisite_id FROM chain ORDER BY depth DESC
                """),
                {"sid": skill_id},
            )
            chain = [r[0] for r in result.fetchall()]
            chain.append(skill_id)
            return chain
        except Exception as e:
            log.warning("Prerequisite chain error for %s: %s", skill_id, e)
            return [skill_id]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_answer(student: str, correct: str, answer_type: str = "text") -> bool:
        """Сравнивает ответы (делегирует общей утилите)."""
        return _shared_check_answer(student, correct, answer_type=answer_type)

    @staticmethod
    def _normalize_answer(answer: str) -> str:
        """Нормализация ответа: strip, lowercase, убрать лишние пробелы."""
        return " ".join(answer.strip().lower().split())

    @staticmethod
    def _deduplicate_gaps(gaps: List[SkillGap]) -> List[SkillGap]:
        """Оставляет один пробел на skill_id с максимальной severity."""
        best: Dict[str, SkillGap] = {}
        for g in gaps:
            if g.skill_id not in best or g.severity > best[g.skill_id].severity:
                best[g.skill_id] = g
        return sorted(best.values(), key=lambda g: g.severity, reverse=True)
