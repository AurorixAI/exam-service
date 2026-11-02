"""
Exam Service — the declarative base of the exam-service ORM models.

The platform tables (tasks, curriculum, textbooks) are not mapped here: they live in content-service and
exam-service reads them over its HTTP API only.
"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
