"""
Exam Service — Exam, Session, and Report repositories (owned tables, read/write).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from src.models import GeneratedExam, ExamSession, ExamReport


class ExamRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, exam: GeneratedExam) -> GeneratedExam:
        self.db.add(exam)
        self.db.commit()
        self.db.refresh(exam)
        return exam

    def get_by_id(self, exam_id: str) -> Optional[GeneratedExam]:
        return self.db.query(GeneratedExam).filter(GeneratedExam.id == exam_id).first()

    def get_by_access_code(self, code: str) -> Optional[GeneratedExam]:
        return (
            self.db.query(GeneratedExam)
            .filter(GeneratedExam.access_code == code)
            .first()
        )

    def publish(self, exam: GeneratedExam, access_code: str) -> GeneratedExam:
        exam.access_code = access_code
        exam.status = "active"
        self.db.commit()
        self.db.refresh(exam)
        return exam

    def close(self, exam: GeneratedExam) -> GeneratedExam:
        exam.status = "closed"
        self.db.commit()
        self.db.refresh(exam)
        return exam

    def update_variants(self, exam: GeneratedExam, variants: list, count: int) -> None:
        exam.variants = variants
        exam.variants_count = count
        exam.status = "variants_ready"
        self.db.commit()


class ExamReportRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_exam(self, exam_id: str) -> Optional[ExamReport]:
        return (
            self.db.query(ExamReport)
            .filter(ExamReport.exam_id == exam_id)
            .first()
        )

    def upsert(
        self,
        exam_id: str,
        report_id: str,
        *,
        total_students: int,
        submitted_count: int,
        avg_score_pct: float,
        pass_rate_pct: float,
        task_analytics: list,
        top_errors: list,
        narrative_ru: Optional[str],
        narrative_generated: bool,
        is_final: bool,
    ) -> ExamReport:
        """Create or fully overwrite the report for an exam (idempotent)."""
        existing = self.get_by_exam(exam_id)
        if existing:
            existing.total_students = total_students
            existing.submitted_count = submitted_count
            existing.avg_score_pct = avg_score_pct
            existing.pass_rate_pct = pass_rate_pct
            existing.task_analytics = task_analytics
            existing.top_errors = top_errors
            existing.narrative_ru = narrative_ru
            existing.narrative_generated = narrative_generated
            existing.is_final = is_final
            existing.updated_at = datetime.now(timezone.utc)
            self.db.commit()
            self.db.refresh(existing)
            return existing

        report = ExamReport(
            id=report_id,
            exam_id=exam_id,
            total_students=total_students,
            submitted_count=submitted_count,
            avg_score_pct=avg_score_pct,
            pass_rate_pct=pass_rate_pct,
            task_analytics=task_analytics,
            top_errors=top_errors,
            narrative_ru=narrative_ru,
            narrative_generated=narrative_generated,
            is_final=is_final,
        )
        self.db.add(report)
        self.db.commit()
        self.db.refresh(report)
        return report


class SessionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, session: ExamSession) -> ExamSession:
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def get_by_id(self, session_id: str) -> Optional[ExamSession]:
        return (
            self.db.query(ExamSession).filter(ExamSession.id == session_id).first()
        )

    def submit(
        self,
        session: ExamSession,
        answers: dict,
        score: int,
        max_score: int,
        correct_count: int,
        total_tasks: int,
        time_spent: int,
        distractor_analytics: dict | None = None,
    ) -> ExamSession:
        session.answers = answers
        session.score = score
        session.max_score = max_score
        session.correct_count = correct_count
        session.total_tasks = total_tasks
        session.time_spent_seconds = time_spent
        session.status = "submitted"
        session.submitted_at = datetime.now(timezone.utc)
        if distractor_analytics is not None:
            session.distractor_analytics = distractor_analytics
        self.db.commit()
        self.db.refresh(session)
        return session
