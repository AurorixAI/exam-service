"""
Exam Service — Exam, Session, and Report repositories (owned tables, read/write).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from src.models import GeneratedExam, ExamSession, ExamReport


class ExamRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, exam: GeneratedExam) -> GeneratedExam:
        self.db.add(exam)
        self.db.commit()
        self.db.refresh(exam)
        return exam

    def _query(self, without_tasks: bool):
        query = self.db.query(GeneratedExam)
        if without_tasks:
            # The task JSON of an exam is large (every variant, every task) and a public entry that only checks the
            # state of the exam does not need it: it is read once, by the step that builds the student's tasks.
            query = query.options(defer(GeneratedExam.variants))
        return query

    def get_by_id(self, exam_id: str, *, without_tasks: bool = False) -> Optional[GeneratedExam]:
        return self._query(without_tasks).filter(GeneratedExam.id == exam_id).first()

    def get_by_access_code(self, code: str, *, without_tasks: bool = False) -> Optional[GeneratedExam]:
        clean = (code or "").strip().upper()
        return (
            self._query(without_tasks)
            .filter(func.upper(GeneratedExam.access_code) == clean)
            .first()
        )

    def publish(self, exam: GeneratedExam, access_code: str) -> GeneratedExam:
        exam.access_code = access_code
        exam.status = "lobby"
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

    def delete(self, exam: GeneratedExam) -> None:
        self.db.query(ExamReport).filter(ExamReport.exam_id == exam.id).delete()
        self.db.query(ExamSession).filter(ExamSession.exam_id == exam.id).delete()
        self.db.delete(exam)
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
        """Create or fully overwrite the report for an exam (idempotent, and safe against a concurrent twin).

        ``exam_id`` is unique: two requests that both saw no report (a double click on «generate») both try to insert,
        and the loser's INSERT violates it.  The insert runs in a SAVEPOINT; on that violation the winner's row is
        read and overwritten instead - the same end state as ``INSERT ... ON CONFLICT (exam_id) DO UPDATE``, on any
        database, with the loser's transaction (and the caller's other work) intact.
        """
        values = dict(
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

        existing = self.get_by_exam(exam_id)
        if existing is None:
            try:
                with self.db.begin_nested():
                    self.db.add(ExamReport(id=report_id, exam_id=exam_id, **values))
                    self.db.flush()
            except IntegrityError:
                existing = self.get_by_exam(exam_id)
                if existing is None:  # not the unique key after all: a real error
                    raise
            else:
                self.db.commit()
                return self.get_by_exam(exam_id)

        for field, value in values.items():
            setattr(existing, field, value)
        existing.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(existing)
        return existing


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
        *,
        commit: bool = True,
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
        if commit:
            self.db.commit()
            self.db.refresh(session)
        return session

    def list_by_exam(self, exam_id: str) -> List[ExamSession]:
        return (
            self.db.query(ExamSession)
            .filter(ExamSession.exam_id == exam_id)
            .order_by(ExamSession.started_at.desc())
            .all()
        )
