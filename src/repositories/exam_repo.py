"""
Exam Service — Exam & Session repositories (owned tables, read/write).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from src.models import GeneratedExam, ExamSession


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
    ) -> ExamSession:
        session.answers = answers
        session.score = score
        session.max_score = max_score
        session.correct_count = correct_count
        session.total_tasks = total_tasks
        session.time_spent_seconds = time_spent
        session.status = "submitted"
        session.submitted_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(session)
        return session
