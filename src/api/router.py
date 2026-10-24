"""
Exam Service — unified API router.
Merges exam_router.py + textbook_exam.py from monolith into one module.

Endpoints:
  TEACHER
    POST /exams/generate              — create draft exam (topics or textbook mode)
    POST /exams/{id}/publish          — publish → access_code
    GET  /exams/{id}                  — exam detail (teacher view)
    GET  /exams                       — list teacher's exams (library)
    POST /exams/{id}/pin              — toggle is_pinned flag
    POST /exams/{id}/clone            — duplicate a template for a new group
    POST /exams/{id}/assign           — assign group_id + optional publish
    GET  /exams/{id}/analytics        — live class analytics (no LLM, instant)
    POST /exams/{id}/report           — generate on-demand LLM narrative report
    GET  /exams/{id}/report           — retrieve cached report
  STUDENT (public, by access_code)
    GET  /exams/by-code/{code}        — exam info
    POST /exams/by-code/{code}/start  — start session
    POST /exam-sessions/{id}/submit   — submit answers
    GET  /exam-sessions/{id}/results  — results
  TEXTBOOK (catalog)
    GET  /textbooks                   — list textbooks
    GET  /textbooks/{id}/toc          — table of contents
    GET  /textbooks/{id}/coverage     — digitization coverage
    GET  /toc/{id}/skills             — skills for a paragraph
"""
from __future__ import annotations

from src.domain.task_display import question_display_fields, report_display_projection

import asyncio
import base64
import hashlib
import logging
import math
import random
import re
import secrets
import string
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional, Union
from uuid import uuid4
from zoneinfo import ZoneInfo

import anyio
import anyio.from_thread
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, inspect as sa_inspect, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core import auth as core_auth
from src.core.auth import (
    ExamActor,
    exam_access_group,
    require_exam_access,
    require_exam_staff,
    require_heavy_staff,
    require_group_access,
    require_student_history_access,
)
from src.core.abuse_limit import EntryTicket, admit_identity_attempt, reset_entry_limits
from src.core.config import get_settings
from src.core.database import get_db, wrote_since_commit
from src.core.exceptions import (
    ExamNotFoundError,
    ExamNotActiveError,
    SessionNotFoundError,
    SessionAlreadySubmittedError,
    NoTasksAvailableError,
)
from src.models import ExamDeletion, ExamLifecycleEvent, ExamPrintPackage, ExamReport, GeneratedExam, ExamSession
from src.repositories.task_repo import TaskRepository, TextbookRepository
from src.repositories.exam_repo import ExamRepository, SessionRepository, ExamReportRepository
from src.domain.choice_options import ordered as order_choices
from src.domain.grading import (
    check_task_answer,
    grade_session_tasks,
    mc_choices,
    regrade_if_stale,
    session_tasks,
    should_show_mc,
    stored_verdicts,
    with_grading,
)
from src.domain.print_package import (
    PrintLayoutValidationError,
    build_default_print_layout,
    build_print_snapshot,
    reconcile_print_layout,
    snapshot_sha256,
    validate_print_layout,
)
from src.domain.print_pdf import render_student_exam_pdf, render_teacher_key_pdf

try:
    from src.domain.variant_orchestrator import VariantOrchestrator
except Exception:  # pragma: no cover
    VariantOrchestrator = None  # type: ignore

log = logging.getLogger("exam_router")

router = APIRouter()


# ═══════════════════════════════════════════════════════════════════════════
# Schemas
# ═══════════════════════════════════════════════════════════════════════════


class GenerateExamRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    # Mode A: by curriculum (skill_ids)
    skill_ids: List[str] = []
    # Mode B: by textbook (toc_ids)
    paragraph_ids: List[int] = []
    textbook_id: Optional[str] = None
    # Group binding
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    class_level: Optional[int] = None
    # Common
    tasks_count: int = Field(10, ge=1, le=50)
    difficulty_distribution: Optional[Dict[str, float]] = None  # easy/medium/hard → %
    time_minutes: int = Field(45, ge=5, le=180)
    variants_count: int = Field(1, ge=1, le=30)
    generate_variants: bool = False
    # Questions always preserve the prepared section order.  Only online
    # multiple-choice options may be shuffled per student.
    shuffle_options: bool = True
    mc_mode: str = "auto"
    delivery_mode: Literal["online", "print", "hybrid"] = "online"
    teacher_name: Optional[str] = None


class PublishExamRequest(BaseModel):
    teacher_name: Optional[str] = None
    subject: Optional[str] = None
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    class_level: Optional[int] = None
    lesson_id: str = Field(..., min_length=1, max_length=100)
    lesson_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class ExamSummaryResponse(BaseModel):
    exam_id: str
    title: str
    status: str
    delivery_mode: str = "online"
    access_code: Optional[str] = None
    tasks_per_variant: int
    variants_count: int
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    class_level: Optional[int] = None
    # Snapshot of the teacher responsible for the exam's group. It is exposed
    # for read-only administration only and confers no control privileges.
    teacher_name: Optional[str] = None
    scheduled_lesson_id: Optional[str] = None
    scheduled_lesson_date: Optional[str] = None
    is_pinned: bool = False
    source_exam_id: Optional[str] = None
    total_sessions: int = 0
    skills_covered: List[str] = []
    # A compact, immutable-to-users audit trail for operational monitoring.
    # The full task snapshot never depends on these entries.
    recent_events: List[Dict[str, Any]] = Field(default_factory=list)
    total_time_minutes: int = 45
    created_at: datetime


class LiveExamDashboardResponse(BaseModel):
    """Small, authoritative live-exam payload for the teacher dashboard.

    This deliberately contains operational information only. Results, scores
    and error analysis belong to the analytics surface, not to the live
    dashboard.
    """

    exam_id: str
    title: str
    status: str
    delivery_mode: str = "online"
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    scheduled_lesson_id: Optional[str] = None
    scheduled_lesson_date: Optional[str] = None
    total_time_seconds: int
    time_remaining_seconds: int
    connected_count: int
    solving_count: int
    submitted_count: int
    started_at: Optional[datetime] = None


class ExamTaskPublic(BaseModel):
    task_id: str
    skill_id: str = ""
    skill_name: str = ""
    difficulty: str = "B"
    text: str
    question_latex: Optional[str] = None
    options: List[str] = []
    options_latex: Optional[List[str]] = None
    image_url: Optional[str] = None
    answer_type: str = "exact_number"
    points: int = 10


class ExamInfoResponse(BaseModel):
    exam_id: str
    title: str
    subject: str
    teacher_name: Optional[str] = None
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    class_level: Optional[int] = None
    total_time_minutes: int
    tasks_per_variant: int
    variants_count: int
    status: str
    time_remaining_seconds: Optional[int] = None
    started_at: Optional[str] = None


class CheckStudentExamRequest(BaseModel):
    identifier: Optional[str] = None
    student_id: Optional[str] = None
    student_name: Optional[str] = None


class CheckStudentExamResponse(BaseModel):
    enrolled: bool
    group_required: bool = False
    group_name: Optional[str] = None
    student: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    already_submitted: bool = False
    session_id: Optional[str] = None


class StartExamRequest(BaseModel):
    student_name: str = Field(..., min_length=1)
    class_level: Optional[int] = None
    student_id: Optional[str] = None
    display_id: Optional[str] = None


class StartExamResponse(BaseModel):
    session_id: str
    variant_number: int
    time_minutes: int
    time_remaining_seconds: Optional[int] = None
    tasks: List[ExamTaskPublic] = []
    status: str = "in_progress"
    already_submitted: bool = False
    saved_answers: Optional[Dict[str, str]] = None
    message: Optional[str] = None
    # Opaque capability required for every request that reads or mutates this
    # concrete attempt. It is never exposed in teacher endpoints.
    session_token: Optional[str] = None


# A typed answer is a number, an expression or a few words.  The request bounds what is stored (and later graded in
# one transaction for the whole class): a longer value is a 422, not a row.  Task ids are checked against the exam
# by ``_validate_answer_keys``; their length and count are bounded here too.
MAX_ANSWER_CHARS = 1000
MAX_ANSWERS_PER_REQUEST = 200
AnswerText = Annotated[str, Field(max_length=MAX_ANSWER_CHARS)]
AnswerTaskId = Annotated[str, Field(max_length=100)]


class SaveProgressRequest(BaseModel):
    answers: Dict[AnswerTaskId, AnswerText] = Field(default_factory=dict, max_length=MAX_ANSWERS_PER_REQUEST)
    time_spent_seconds: Optional[int] = None


class SubmitExamRequest(BaseModel):
    answers: Dict[AnswerTaskId, AnswerText] = Field(max_length=MAX_ANSWERS_PER_REQUEST)  # {task_id: answer_string}
    time_spent_seconds: int = 0


class SubmitExamResponse(BaseModel):
    session_id: str
    score: int
    max_score: int
    answered_count: int
    correct_count: int
    total_tasks: int
    percentage: float


class DistractorHit(BaseModel):
    distractor_value: str
    error_type: str
    explanation: str


class TaskResultItem(BaseModel):
    task_id: str
    text: str
    question_latex: Optional[str] = None
    student_answer: Optional[str]
    # Key-revealing fields are None while the exam is still running for
    # classmates (see get_results / answer_key_available).
    correct_answer: Optional[str] = None
    correct_answer_latex: Optional[str] = None
    is_correct: Optional[bool] = None
    options: List[str] = []
    skill_name: str = ""
    distractor_hit: Optional[DistractorHit] = None


class SessionResultResponse(BaseModel):
    session_id: str
    exam_title: str
    student_name: str
    variant_number: int
    score: int
    max_score: int
    answered_count: int
    correct_count: int
    total_tasks: int
    percentage: float
    time_spent_seconds: int
    submitted_at: Optional[datetime]
    # False until the whole exam is over: the key, per-task verdicts and
    # explanations are withheld so an early finisher cannot leak them.
    answer_key_available: bool = False
    message: Optional[str] = None
    tasks: List[TaskResultItem] = []


class TextbookInfo(BaseModel):
    textbook_id: str
    title: str
    authors: List[str] = []
    class_level: int
    digitization_status: str = "pending"
    digitization_progress: float = 0.0
    tasks_extracted: int = 0
    cover_image_url: Optional[str] = None


class TextbookDetail(TextbookInfo):
    subtitle: Optional[str] = None
    publisher: Optional[str] = None
    isbn: Optional[str] = None
    edition: Optional[str] = None
    country: str = "KZ"
    language: str = "ru"
    total_pages: Optional[int] = None
    is_active: bool = True


class TocNode(BaseModel):
    id: int
    parent_id: Optional[int] = None
    level: Union[str, int] = 1
    number: Optional[str] = None
    title: str = ""
    display_name: Optional[str] = None
    display_number: Optional[str] = None
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    sort_order: int = 0
    tasks_count: int = 0
    children: List["TocNode"] = []

    def model_post_init(self, __context: Any) -> None:
        if not self.display_name:
            self.display_name = self.title
        if not self.display_number and self.number:
            self.display_number = self.number


class SkillTreeNode(BaseModel):
    id: str
    level: Union[str, int] = "L4"
    name: Optional[str] = None
    name_ru: Optional[str] = ""
    name_uz: Optional[str] = None
    parent_id: Optional[str] = None
    class_level_start: Optional[int] = None
    class_level_end: Optional[int] = None
    importance: int = 5
    sequence_order: Optional[int] = 0
    tasks_count: int = 0
    children: List["SkillTreeNode"] = []

    def model_post_init(self, __context: Any) -> None:
        if not self.name:
            self.name = self.name_ru or self.id
        if not self.name_ru:
            self.name_ru = self.name or self.id


class TextbookTaskInfo(BaseModel):
    task_id: str
    toc_id: Optional[int] = None
    skill_id: str
    difficulty: str = "B"
    question_text: str
    question_latex: Optional[str] = None
    answer_type: str = "exact_number"
    has_template: bool = False


class SkillInParagraph(BaseModel):
    skill_id: str
    skill_name: str
    role: str = "primary"
    confidence: float = 0.8
    is_main: bool = False
    tasks_count: int = 0


class CoverageItem(BaseModel):
    toc_id: int
    level: Union[str, int] = 1
    number: Optional[str] = None
    title: str = ""
    skills_mapped: int = 0
    tasks_available: int = 0
    is_digitized: bool = True


class PublishExamResponse(BaseModel):
    exam_id: str
    title: str
    status: str
    access_code: str
    tasks_per_variant: int
    variants_count: int
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    class_level: Optional[int] = None
    created_at: datetime
    url: str


class DraftTask(BaseModel):
    task_id: str
    skill_id: str = ""
    skill_name: str = ""
    difficulty: str = "B"
    question_text: str
    question_latex: Optional[str] = None
    question_image_url: Optional[str] = None
    answer_type: str = "exact_number"
    correct_answer: str = ""
    correct_answer_latex: Optional[str] = None
    answer_options: Optional[List[str]] = None
    distractor_meta: Optional[List[dict]] = None
    is_template_generated: bool = False


class DraftVariant(BaseModel):
    variant_id: str
    variant_number: int
    total_points: int
    tasks: List[DraftTask]


class DraftExamResponse(BaseModel):
    exam_id: str
    title: str
    status: str
    delivery_mode: str = "online"
    access_code: Optional[str] = None
    total_time_minutes: int
    variants_count: int
    tasks_per_variant: int
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    class_level: Optional[int] = None
    is_pinned: bool = False
    scheduled_lesson_id: Optional[str] = None
    scheduled_lesson_date: Optional[str] = None
    settings: Dict[str, Any]
    # Only lifecycle metadata is returned.  The snapshot and answer key remain
    # server-side and are rendered directly into staff PDFs.
    print_package: Optional[Dict[str, Any]] = None
    skills_covered: List[str]
    created_at: datetime
    variants: List[DraftVariant]


class UpdatePrintLayoutRequest(BaseModel):
    """Full printable layout sent by the draft editor before packaging."""

    layout: Dict[str, Any]


class PreparePrintPackageRequest(BaseModel):
    """Bind an offline package to exactly one scheduled lesson occurrence."""

    group_id: str = Field(..., min_length=1, max_length=36)
    lesson_id: str = Field(..., min_length=1, max_length=100)
    lesson_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class AdminExamTask(BaseModel):
    """Read-only task snapshot for an organisation administrator.

    Correct answers and pedagogical distractor analysis are deliberately
    omitted while a public exam is in lobby/active state.  The task itself is
    still visible, so administration can verify the paper without gaining a
    way to leak an answer key during a run.
    """

    task_id: str
    skill_id: str = ""
    skill_name: str = ""
    difficulty: str = "B"
    question_text: str
    question_latex: Optional[str] = None
    question_image_url: Optional[str] = None
    answer_type: str = "exact_number"
    answer_options: Optional[List[str]] = None
    correct_answer: Optional[str] = None
    correct_answer_latex: Optional[str] = None
    distractor_meta: Optional[List[dict]] = None


class AdminExamVariant(BaseModel):
    variant_id: str
    variant_number: int
    total_points: int
    tasks: List[AdminExamTask]


class AdminExamViewResponse(ExamSummaryResponse):
    """Administrator-visible exam profile; never contains a student code."""

    answer_key_available: bool = False
    variants: List[AdminExamVariant]
    lifecycle_events: List[Dict[str, Any]] = Field(default_factory=list)


class AdminExamMonitorResponse(BaseModel):
    """One page of the organisation's historical exam monitor."""

    items: List[ExamSummaryResponse]
    total: int
    limit: int
    offset: int
    period_from: Optional[str] = None
    period_to: Optional[str] = None
    counts: Dict[str, int] = Field(default_factory=dict)


class ReplaceTaskRequest(BaseModel):
    variant_number: int = 1
    task_id: str
    old_task_id: Optional[str] = None
    new_task_id: Optional[str] = None


class LessonBindingRequest(BaseModel):
    """The stable identity of one resolved schedule occurrence."""

    lesson_id: str = Field(..., min_length=1, max_length=100)
    lesson_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class CancelExamDeliveryRequest(BaseModel):
    """Teacher's explicit decision not to conduct an unstarted delivery."""

    reason: str = Field(..., min_length=3, max_length=500)


class LessonCancellationSyncRequest(LessonBindingRequest):
    org_id: str = Field(..., min_length=1, max_length=36)
    group_id: str = Field(..., min_length=1, max_length=36)
    reason: Optional[str] = Field(default=None, max_length=500)


class LessonCompletionSyncRequest(LessonBindingRequest):
    """Internal: auth-service asks about / reports the END of a lesson (LOGIC-B O8)."""

    org_id: str = Field(..., min_length=1, max_length=36)
    group_id: str = Field(..., min_length=1, max_length=36)
    actor_id: Optional[str] = Field(default=None, max_length=100)


class GroupBindingSyncRequest(BaseModel):
    """Internal: auth-service archives a group (LOGIC-B O4); the exams of the group are looked at / released."""

    org_id: str = Field(..., min_length=1, max_length=36)
    group_id: str = Field(..., min_length=1, max_length=36)
    actor_id: Optional[str] = Field(default=None, max_length=100)
    reason: Optional[str] = Field(default=None, max_length=500)


class RecurringLessonEndSyncRequest(BaseModel):
    """Internal request to stop one recurring schedule from a future date."""

    org_id: str = Field(..., min_length=1, max_length=36)
    group_id: str = Field(..., min_length=1, max_length=36)
    group_name: Optional[str] = Field(default=None, max_length=200)
    lesson_id: str = Field(..., min_length=1, max_length=100)
    effective_from: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    actor_id: Optional[str] = Field(default=None, max_length=100)
    reason: Optional[str] = Field(default=None, max_length=500)


class LessonRescheduleSyncRequest(LessonBindingRequest):
    """Internal immutable audit payload for a one-off lesson transfer."""

    org_id: str = Field(..., min_length=1, max_length=36)
    group_id: str = Field(..., min_length=1, max_length=36)
    group_name: Optional[str] = Field(default=None, max_length=200)
    actor_id: Optional[str] = Field(default=None, max_length=100)
    reason: Optional[str] = Field(default=None, max_length=500)
    previous: Dict[str, Any] = Field(default_factory=dict)
    current: Dict[str, Any] = Field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _generate_access_code(db: Optional[Session] = None) -> str:
    """A fresh public code.  With ``db`` it is checked against the codes already issued (the column is unique), so a
    collision is re-rolled here instead of surfacing at commit as a misleading «lesson already has an exam»."""
    chars = string.ascii_uppercase + string.digits
    code = "EX" + "".join(random.choices(chars, k=8))
    if db is None:
        return code
    for _ in range(20):
        taken = db.query(GeneratedExam.id).filter(func.upper(GeneratedExam.access_code) == code).first()
        if taken is None:
            return code
        code = "EX" + "".join(random.choices(chars, k=8))
    return code


def _new_exam_id() -> str:
    """64 random bits (the former 32 collided at ~77 000 exams); ``String(30)`` holds the 21 characters."""
    return f"EXAM_{uuid4().hex[:16]}"


def _add_new_exam(db: Session, exam: GeneratedExam, *, attempts: int = 5) -> None:
    """``db.add`` + ``flush`` of a brand-new exam; an id collision draws a new id instead of ending in a 500.

    The flush runs in a SAVEPOINT so a lost draw does not poison the caller's transaction.
    """
    for attempt in range(attempts):
        try:
            with db.begin_nested():
                db.add(exam)
                db.flush()
            return
        except IntegrityError:
            if attempt == attempts - 1:
                raise
            exam.id = _new_exam_id()


def _binding_conflict(exc: IntegrityError, *, package_exists_message: bool = False) -> HTTPException:
    """Which unique constraint was hit decides the message: a taken code or a twin request is not «the lesson already has an exam»."""
    text = str(getattr(exc, "orig", exc)).lower()
    if "access_code" in text:
        return HTTPException(409, "Не удалось выдать код доступа к экзамену. Повторите попытку")
    if package_exists_message and "exam_print_packages" in text:
        return HTTPException(409, "Печатный комплект для этого экзамена уже подготовлен")
    return HTTPException(409, "На это занятие уже назначен другой открытый экзамен")


def _generate_session_token() -> str:
    """Return a high-entropy capability, not an enumerable session id."""
    return secrets.token_urlsafe(32)


async def _run_awaitable(awaitable):
    return await awaitable


def _has_pending_changes(db: Session) -> bool:
    return bool(db.new or db.deleted or any(db.is_modified(obj) for obj in db.dirty))


def _release_read_transaction(db: Session) -> None:
    """End the read transaction - the pooled connection goes back to the pool - WITHOUT expiring what was loaded.

    The ORM objects stay usable (a plain ``commit()`` would expire them and every attribute read would be a new query);
    the next statement simply starts a new transaction.  Only for a session with nothing pending.
    """
    keep = db.expire_on_commit
    db.expire_on_commit = False
    try:
        db.commit()
    finally:
        db.expire_on_commit = keep


def _await(awaitable, db: Optional[Session] = None):
    """Run a coroutine - a call to auth-service / content-service - on the event loop, from a worker thread.

    The teacher and admin routes are plain ``def`` (FastAPI runs them in the thread pool), so their database work
    never blocks the event loop; the few network calls they make are handed to the loop through this bridge.  Rules
    (pinned by tests/test_concurrency_safety.py and test_staff_routes_safety.py):

    * pass the route's ``db``: the read transaction is ended first, so the pooled connection is not held idle in
      a transaction for the seconds the network call may take (ROADMAP #95 R3: pool and idle-in-transaction limit);
    * nothing may be pending (unflushed ORM changes) nor written since the last commit (a flush, a bulk
      UPDATE/DELETE/INSERT, a SELECT ... FOR UPDATE - the row locks would be held over the call).  The testing
      profile raises; production logs an ERROR, does NOT commit (nothing is half-saved: the route's own commit or
      the request's rollback decides) and still makes the call;
    * the coroutine does no database work and is given snapshots (``_exam_view``), never attached ORM objects.
    """
    if isinstance(db, Session):
        if _has_pending_changes(db) or wrote_since_commit(db):
            if get_settings().app_env == "testing":
                if hasattr(awaitable, "close"):
                    awaitable.close()
                raise RuntimeError("a bridged network call was made with ORM changes pending or written since the last commit")
            log.error("bridged network call over uncommitted writes or row locks: the transaction stays open over it")
        else:
            _release_read_transaction(db)
    return anyio.from_thread.run(_run_awaitable, awaitable)


async def _group_access_batch(actor: ExamActor, group_ids: list[str]) -> dict:
    """One concurrent round of group lookups (a few at a time): {group_id: group dict | the exception it raised}."""
    gate = asyncio.Semaphore(8)

    async def one(group_id: str):
        async with gate:
            return await core_auth.require_group_access(actor, group_id)

    results = await asyncio.gather(*(one(group_id) for group_id in group_ids), return_exceptions=True)
    return dict(zip(group_ids, results))


async def _substitute_lessons_batch(actor: ExamActor, group_ids: list[str]) -> dict:
    """{group_id: {(lesson_id, lesson_date)} | None}: auth-service's lesson-access of several groups at once, a few at a time."""
    gate = asyncio.Semaphore(8)

    async def one(group_id: str):
        async with gate:
            return await core_auth.substitute_lessons(actor, group_id)

    results = await asyncio.gather(*(one(group_id) for group_id in group_ids))
    return dict(zip(group_ids, results))


def _exam_access_decisions(db: Session, exams: list, actor: ExamActor) -> dict[str, bool]:
    """{exam id: may this actor see it} for a LIST of exams, with ONE concurrent round of group lookups.

    Same rule as ``require_exam_access`` per exam (a refusal 403/404 hides the exam, anything else is raised), but the
    distinct groups are asked about together, after the read transaction was ended, not one after another with the
    pooled connection held.
    """
    decisions: dict[str, bool] = {}
    by_group: dict[str, list[str]] = {}
    for exam in exams:
        try:
            group_id = exam_access_group(exam, actor)
        except HTTPException as exc:
            if exc.status_code not in {403, 404}:
                raise
            decisions[exam.id] = False
            continue
        if group_id is None:
            decisions[exam.id] = True
        else:
            by_group.setdefault(group_id, []).append(exam.id)
    if by_group:
        outcomes = _await(_group_access_batch(actor, list(by_group)), db)
        by_id = {exam.id: exam for exam in exams}
        refused: dict[str, bool] = {}
        for group_id, outcome in outcomes.items():
            if isinstance(outcome, HTTPException):
                if outcome.status_code not in {403, 404}:
                    raise outcome
                refused[group_id] = outcome.status_code == 403
            elif isinstance(outcome, BaseException):
                raise outcome
        # LOGIC-B2 (D-1): a substitute is refused on the group but may see the exam of the lesson he substitutes in.  Ask auth-service only for the refused
        # groups that hold at least one exam bound to a lesson, all at once (bounded), not one 5 s call after another.
        to_check = [
            group_id for group_id, forbidden in refused.items()
            if forbidden and actor.role == "teacher" and any(core_auth.exam_lesson_key(by_id.get(exam_id)) for exam_id in by_group[group_id])
        ]
        substituted = _await(_substitute_lessons_batch(actor, to_check), db) if to_check else {}
        for group_id in outcomes:
            allowed = group_id not in refused
            lessons = substituted.get(group_id)
            for exam_id in by_group[group_id]:
                if not allowed and lessons:
                    key = core_auth.exam_lesson_key(by_id.get(exam_id))
                    decisions[exam_id] = bool(key and key in lessons)
                else:
                    decisions[exam_id] = allowed
    return decisions


def _get_exam_for_actor(db: Session, exam_id: str, actor: ExamActor, op: Optional[str] = None) -> GeneratedExam:
    """``op`` names the operation for the substitute allow-list (``core.auth.SUBSTITUTE_EXAM_OPS``); a route that does not name one is closed to a substitute."""
    exam = ExamRepository(db).get_by_id(exam_id)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    _await(require_exam_access(_exam_view(exam), actor, op), db)
    return exam


def _require_teacher_exam_operator(actor: ExamActor) -> None:
    """Keep the staff roles intentionally separate.

    Administrators need organisation-wide *visibility* into an exam, but the
    lifecycle of a classroom run belongs to the teacher responsible for that
    group.  Hiding a button in the web UI is not a permission boundary, so the
    API applies the same rule to every state-changing operation.
    """
    if actor.role != "teacher":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Управлять проведением экзамена может только преподаватель группы",
        )


def _require_exam_admin_observer(actor: ExamActor) -> None:
    """Administrative monitoring is organisation-wide and strictly read-only."""
    if not actor.is_org_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Просмотр мониторинга экзаменов доступен только администратору")


def _get_exam_for_teacher_operator(
    db: Session,
    exam_id: str,
    actor: ExamActor,
    op: Optional[str] = None,
) -> GeneratedExam:
    _require_teacher_exam_operator(actor)
    return _get_exam_for_actor(db, exam_id, actor, op)


_BUSINESS_TIMEZONE = ZoneInfo("Asia/Tashkent")


def _business_now() -> datetime:
    """ONE clock reading in business time (Asia/Tashkent): take the date and the minutes from the same value."""
    return datetime.now(_BUSINESS_TIMEZONE)


def _business_today() -> date:
    return _business_now().date()


def _has_any_sessions(db: Session, exam_id: str) -> bool:
    """A started/waiting attempt makes the run historically immutable."""
    return db.query(ExamSession.id).filter(ExamSession.exam_id == exam_id).first() is not None


def _lock_exam_for_transition(
    db: Session,
    exam: GeneratedExam,
    allowed_statuses: set[str] | frozenset[str],
    refusal: str,
    *,
    no_sessions_refusal: Optional[str] = None,
) -> GeneratedExam:
    """Take the exam's row lock and re-check the state the transition was decided on.

    A status change is read -> (network) -> write.  Two requests (a double click) both passed the first read; the
    row lock makes the second wait and then see the first one's result, so it answers 409 instead of repeating the
    transition.  Call it AFTER the last network call (the bridge refuses to run over a row lock) and BEFORE the first
    change.  Lock order everywhere: exam -> sessions.  ``populate_existing`` refreshes ``exam`` itself.
    """
    locked = _lock_exam_row(db, exam)
    if locked.status not in allowed_statuses:
        db.rollback()  # nothing changed: rollback releases the row lock
        raise HTTPException(409, refusal)
    if no_sessions_refusal and _has_any_sessions(db, locked.id):
        db.rollback()
        raise HTTPException(409, no_sessions_refusal)
    return locked


def _require_print_delivery(exam: GeneratedExam) -> None:
    if exam.delivery_mode != "print":
        raise HTTPException(409, "Эта операция доступна только для экзамена в формате «Печать»")


def _print_package_metadata(db: Session, exam_id: str) -> Optional[dict[str, Any]]:
    """Expose lifecycle metadata, never the protected answer-key snapshot."""
    package = db.query(ExamPrintPackage).filter(ExamPrintPackage.exam_id == exam_id).first()
    if not package:
        return None
    return {
        "status": package.status,
        "version": package.version,
        "snapshot_sha256": package.snapshot_sha256,
        "created_at": package.created_at.isoformat() if package.created_at else None,
        "released_at": package.released_at.isoformat() if package.released_at else None,
    }


def _get_print_package_or_404(db: Session, exam_id: str) -> ExamPrintPackage:
    package = db.query(ExamPrintPackage).filter(ExamPrintPackage.exam_id == exam_id).first()
    if not package:
        raise HTTPException(404, "Печатный комплект для этого экзамена ещё не подготовлен")
    return package


_PRINT_FIGURE_PREFIX = "/api/v1/figures/"
_PRINT_FIGURE_CONTENT_TYPES = {"image/png", "image/jpeg"}
_MAX_PRINT_FIGURE_BYTES = 8 * 1024 * 1024


def _snapshot_print_tasks(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every task once from a print snapshot's canonical flat lists."""
    return [
        task
        for variant in snapshot.get("variants") or []
        if isinstance(variant, dict)
        for task in variant.get("tasks") or []
        if isinstance(task, dict)
    ]


def _internal_figure_url(reference: str) -> str:
    """Resolve a task image only from the first-party figures service.

    Print PDFs are made server-side.  Accepting an arbitrary source URL here
    would turn a teacher draft into an SSRF primitive, so the package accepts
    only the relative, content-service-owned figure paths created by ALGO.
    """
    path = reference.strip()
    if not path.startswith(_PRINT_FIGURE_PREFIX) or "/../" in path or path.endswith("/.."):
        raise HTTPException(422, "Для печати доступен только рисунок из библиотеки ALGO")
    return f"{get_settings().content_service_url.rstrip('/')}{path}"


async def _embed_print_figures(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Embed verified figure bytes into a preview or immutable package.

    The final package stores the exact PNG/JPEG bytes and their checksum.  A
    later replacement of a source image therefore cannot silently change an
    already approved paper or teacher key.
    """
    tasks = [
        task
        for task in _snapshot_print_tasks(snapshot)
        if str(task.get("question_image_url") or "").strip()
    ]
    if not tasks:
        return snapshot

    cache: dict[str, dict[str, str]] = {}
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
            for task in tasks:
                reference = str(task["question_image_url"]).strip()
                image = cache.get(reference)
                if image is None:
                    response = await client.get(_internal_figure_url(reference), headers={"accept": "image/png,image/jpeg"})
                    if response.status_code == 404:
                        raise HTTPException(422, f"Рисунок для задания {task.get('task_id') or 'без ID'} не найден")
                    if response.status_code != 200:
                        raise HTTPException(503, "Не удалось получить рисунок для печатного бланка. Повторите попытку.")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
                    content = response.content
                    if content_type not in _PRINT_FIGURE_CONTENT_TYPES:
                        raise HTTPException(422, f"Рисунок для задания {task.get('task_id') or 'без ID'} должен быть PNG или JPEG")
                    if not content or len(content) > _MAX_PRINT_FIGURE_BYTES:
                        raise HTTPException(422, f"Размер рисунка для задания {task.get('task_id') or 'без ID'} недопустим для печати")
                    if content_type == "image/png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise HTTPException(422, f"Файл рисунка для задания {task.get('task_id') or 'без ID'} повреждён")
                    if content_type == "image/jpeg" and not content.startswith(b"\xff\xd8"):
                        raise HTTPException(422, f"Файл рисунка для задания {task.get('task_id') or 'без ID'} повреждён")
                    image = {
                        "content_type": content_type,
                        "data_base64": base64.b64encode(content).decode("ascii"),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                    cache[reference] = image
                task["question_image"] = image
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Не удалось получить рисунок для печатного бланка. Повторите попытку.") from exc
    return snapshot


def _build_print_preview_snapshot(exam: GeneratedExam) -> dict[str, Any]:
    """Build a non-persistent PDF source for the print editor.

    Unlike a package snapshot it has no assigned lesson and is never written
    to the database.  This keeps preview safe to use repeatedly while the
    teacher is still deciding the answer format and the target group.
    """
    try:
        layout = validate_print_layout(
            exam.variants or [],
            (exam.settings or {}).get("print_layout") or build_default_print_layout(exam.variants or []),
        )
        snapshot = build_print_snapshot(
            exam_id=exam.id,
            title=exam.title,
            subject=exam.subject or "Математика",
            group_name=exam.group_name,
            class_level=exam.class_level,
            lesson_id="preview",
            lesson_date=_business_today().isoformat(),
            total_time_minutes=exam.total_time_minutes or 45,
            variants=exam.variants or [],
            layout=layout,
        )
    except PrintLayoutValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return snapshot


def _event_payload(event: ExamLifecycleEvent) -> dict:
    """Serialize one append-only event for staff-facing monitoring."""
    details = event.details if isinstance(event.details, dict) else {}
    return {
        "type": event.event_type,
        "at": event.occurred_at.isoformat() if event.occurred_at else None,
        "lesson_id": event.lesson_id,
        "lesson_date": event.lesson_date,
        "previous_lesson_id": event.previous_lesson_id,
        "previous_lesson_date": event.previous_lesson_date,
        "group_id": event.group_id,
        "group_name": event.group_name,
        "reason": event.reason,
        "details": details,
    }


def _recent_exam_lifecycle_events(db: Session, exam: GeneratedExam, *, limit: int = 12) -> list[dict]:
    """Prefer immutable database history; retain legacy JSON as a safe fallback.

    The fallback makes rolling deployment backward-compatible for existing
    rows. New operational events are always written to ``exam_lifecycle_events``.
    """
    persisted = (
        db.query(ExamLifecycleEvent)
        .filter(ExamLifecycleEvent.exam_id == exam.id)
        .order_by(ExamLifecycleEvent.occurred_at.desc(), ExamLifecycleEvent.id.desc())
        .limit(limit)
        .all()
    )
    if persisted:
        return [_event_payload(event) for event in reversed(persisted)]
    raw = (exam.settings or {}).get("lifecycle_events", [])
    if not isinstance(raw, list):
        return []
    return [event for event in raw[-limit:] if isinstance(event, dict)]


def _record_exam_lifecycle_event(
    db: Session,
    exam: GeneratedExam,
    event: dict,
    *,
    actor_id: Optional[str] = None,
) -> None:
    """Append an immutable operational event without changing task content."""
    occurred_at = datetime.utcnow()
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    persisted = ExamLifecycleEvent(
        id=str(uuid4()),
        exam_id=exam.id,
        org_id=str(exam.org_id or event.get("org_id") or ""),
        event_type=str(event.get("type") or "updated"),
        occurred_at=occurred_at,
        actor_id=actor_id,
        group_id=str(event.get("group_id") or exam.group_id or "") or None,
        group_name=event.get("group_name") or exam.group_name,
        lesson_id=event.get("lesson_id"),
        lesson_date=event.get("lesson_date"),
        previous_lesson_id=event.get("previous_lesson_id"),
        previous_lesson_date=event.get("previous_lesson_date"),
        reason=(str(event.get("reason")).strip()[:500] if event.get("reason") else None),
        details=details,
    )
    db.add(persisted)

    # A bounded copy is kept only for a short rolling-deployment transition.
    # It is never used as the authoritative source for historical filtering.
    settings = dict(exam.settings or {})
    history = settings.get("lifecycle_events", [])
    if not isinstance(history, list):
        history = []
    safe_event = {
        "type": persisted.event_type,
        "at": occurred_at.isoformat(),
        "lesson_id": persisted.lesson_id,
        "lesson_date": persisted.lesson_date,
        "previous_lesson_id": persisted.previous_lesson_id,
        "previous_lesson_date": persisted.previous_lesson_date,
        "group_id": persisted.group_id,
        "group_name": persisted.group_name,
        "reason": persisted.reason,
        "details": details,
    }
    settings["lifecycle_events"] = [*history[-19:], safe_event]
    exam.settings = settings


def _reset_lobby_to_draft(
    db: Session,
    exam: GeneratedExam,
    *,
    lifecycle_event: Optional[dict] = None,
    actor_id: Optional[str] = None,
) -> None:
    """Revoke public access without touching the immutable task snapshot.

    The group remains selected so a teacher can move the same draft to the
    next lesson.  Only the concrete occurrence and access code are cleared.
    """
    if lifecycle_event:
        _record_exam_lifecycle_event(db, exam, lifecycle_event, actor_id=actor_id)
    exam.status = "variants_ready" if (exam.variants and len(exam.variants) > 0) else "draft"
    exam.access_code = None
    exam.scheduled_lesson_id = None
    exam.scheduled_lesson_date = None
    settings = dict(exam.settings or {})
    settings.pop("started_at", None)
    exam.settings = settings


def _void_print_package_for_cancelled_lesson(
    db: Session,
    exam: GeneratedExam,
    *,
    reason: Optional[str] = None,
    actor_id: Optional[str] = None,
    disposition: str = "lesson_cancelled",
) -> None:
    """Retire a prepared paper when its bound delivery is cancelled.

    A printable package includes the concrete lesson date in its signed
    snapshot.  Rebinding it would make a previously downloaded paper look as
    though it had been prepared for another lesson.  We keep the audit row,
    return the exam to an editable state, and require a deliberate clone for
    another delivery.
    """
    package = db.query(ExamPrintPackage).filter(ExamPrintPackage.exam_id == exam.id).first()
    if package:
        package.status = "voided"
    _reset_lobby_to_draft(
        db,
        exam,
        actor_id=actor_id,
        lifecycle_event={
            "type": "print_package_voided",
            "group_id": exam.group_id,
            "group_name": exam.group_name,
            "lesson_id": exam.scheduled_lesson_id,
            "lesson_date": exam.scheduled_lesson_date,
            "reason": reason,
            "details": {"reason": disposition, "package_version": package.version if package else None},
        },
    )


def _require_bound_exam(exam: GeneratedExam) -> None:
    if not exam.group_id or not exam.scheduled_lesson_id or not exam.scheduled_lesson_date:
        raise HTTPException(
            409,
            "Экзамен не привязан к конкретному занятию. Выберите урок в расписании перед открытием лобби.",
        )


def _invalidate_unbound_open_exam(db: Session, exam: GeneratedExam) -> bool:
    """Quarantine only invalid legacy open states, never student attempts."""
    if exam.status in {"lobby", "active"} and (
        not exam.group_id or not exam.scheduled_lesson_id or not exam.scheduled_lesson_date
    ):
        # A legacy active run with attempts cannot be silently rewritten. It
        # remains visible to staff for a deliberate close, but never remains a
        # public entry point without an occurrence binding.
        if _has_any_sessions(db, exam.id):
            return False
        _reset_lobby_to_draft(db, exam)
        db.commit()
        db.refresh(exam)
        return True
    return False


LESSON_OVER_MESSAGE = "Занятие уже прошло или отменено — выберите другое"


_JS_NUMBER = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _js_number(text: str) -> float:
    """``Number(text)`` of JavaScript, as far as the dialog's ``clockMinutes`` needs it: never raises.

    Blank is 0; a plain decimal (``1e999`` is Infinity, as in JS); ``Infinity``; everything else - ``inf``, ``nan``,
    ``1_0``, hex, Unicode digits, words - is NaN.
    """
    text = text.strip()
    if text == "":
        return 0.0
    if text in ("Infinity", "+Infinity"):
        return math.inf
    if text == "-Infinity":
        return -math.inf
    return float(text) if _JS_NUMBER.fullmatch(text) else math.nan


def _clock_minutes(value: Any) -> Optional[float]:
    """Minutes since midnight of an «HH:MM» end time, read the way the frontend reads it (``clockMinutes``).

    No end time at all (None, empty or blank) is None: the lesson is not judged over.  A malformed one is read as 0 -
    the frontend does the same (``Number("x") || 0``), so such a lesson is already over and the dialog hides it.
    The result is a float so that the odd ones behave as in JS too: Infinity is never over, NaN always is.
    Nothing here raises: an end time that cannot be read is a lesson that is over, never a 500.
    """
    if value is None or str(value).strip() == "":
        return None

    def number(part: Optional[str]) -> float:
        parsed = 0.0 if part is None else _js_number(part)
        return 0.0 if parsed != parsed else parsed  # NaN reads as 0

    parts = str(value).split(":")
    hours = number(parts[0])
    minutes = number(parts[1]) if len(parts) > 1 else 0.0
    return hours * 60 + minutes


def _lesson_is_bindable(
    *,
    lesson_date: str,
    end_time: Any,
    lesson_status: Optional[str],
    today: date,
    now_minutes: int,
) -> bool:
    """Whether an exam may still be bound to this occurrence - the rule of the frontend dialog.

    Mirrors ``isLessonBindable`` in algo-front ``src/lib/exam-lessons.ts``: a completed or cancelled lesson, a lesson
    of an earlier day, and a lesson of today whose planned end has passed without it being in progress are not
    offered.  A lesson in progress stays bindable even past its planned end (the teacher may run over), a later
    day is bindable, and a lesson without an end time is not judged over (a malformed one is read as 0: over).
    """
    if lesson_status in ("completed", "cancelled"):
        return False
    lesson_day = date.fromisoformat(lesson_date)
    if lesson_day < today:
        return False
    if lesson_day > today:
        return True
    if lesson_status == "in_progress":
        return True
    end_minutes = _clock_minutes(end_time)
    if end_minutes is None:
        return True
    return now_minutes < end_minutes


async def _lesson_attendance_status(
    actor: ExamActor, *, group_id: str, lesson_id: str, lesson_date: str
) -> Optional[str]:
    """The attendance status of one occurrence at auth-service (None = the lesson was never started)."""
    url = f"{get_settings().auth_service_url.rstrip('/')}/api/v1/auth/org/groups/{group_id}/attendance"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                url,
                params={"date_from": lesson_date, "date_to": lesson_date},
                headers=dict(actor.auth_headers),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Не удалось проверить статус урока. Повторите попытку.") from exc
    if response.status_code != 200:
        raise HTTPException(503, "Не удалось проверить статус урока")
    statuses = response.json().get("statuses") or {}
    return statuses.get(f"{lesson_date}_{lesson_id}")


async def _resolve_lesson_occurrence(
    actor: ExamActor,
    *,
    group_id: str,
    lesson_id: str,
    lesson_date: str,
    require_current_or_future: bool = True,
    substitute_bound_lesson: Optional[tuple] = None,
) -> tuple[dict, dict]:
    """Return a canonical, accessible occurrence from auth-service.

    ``substitute_bound_lesson`` = ``(lesson_id, lesson_date)`` the exam is ALREADY bound to: only then may the substitute of that very lesson open its lobby
    (a future/current lesson); he can neither bind an exam to a lesson nor move it to another one.

    The browser may choose an item, but it cannot invent its group, date or
    schedule id.  A transferred lesson keeps the same id/date pair, so this
    lookup naturally follows its new time, room and topic.
    """
    try:
        parsed_date = date.fromisoformat(lesson_date)
    except ValueError as exc:
        raise HTTPException(422, "Дата урока должна иметь формат YYYY-MM-DD") from exc
    if require_current_or_future and parsed_date < _business_today():
        raise HTTPException(409, LESSON_OVER_MESSAGE)

    try:
        group = await require_group_access(actor, group_id)
    except HTTPException as exc:
        # LOGIC-B2 (D-1): the check "is the lesson of this exam in progress" (``require_current_or_future=False``, the start of a lesson exam) is open to the teacher who
        # SUBSTITUTES in exactly this lesson; nothing else of the group is.
        own_lesson = substitute_bound_lesson is not None and tuple(map(str, substitute_bound_lesson)) == (str(lesson_id), str(lesson_date))
        access = None if ((require_current_or_future and not own_lesson) or exc.status_code != 403 or actor.role != "teacher") else await core_auth.lesson_access(actor, group_id)
        if not access or (lesson_id, lesson_date) not in {(str(l.get("lessonId")), str(l.get("lessonDate"))) for l in access.get("lessons") or []}:
            raise
        group = access
    url = f"{get_settings().auth_service_url.rstrip('/')}/api/v1/auth/org/schedule/occurrences"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                url,
                params={"date_from": lesson_date, "date_to": lesson_date},
                headers=dict(actor.auth_headers),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Не удалось проверить занятие в расписании. Повторите попытку.") from exc

    if response.status_code in {401, 403}:
        raise HTTPException(403, "Нет доступа к выбранному занятию")
    if response.status_code != 200:
        raise HTTPException(503, "Не удалось проверить занятие в расписании")

    occurrence = next(
        (
            item
            for item in response.json()
            if str(item.get("id")) == lesson_id
            and str(item.get("groupId")) == str(group["id"])
            and item.get("occurrenceDate") == lesson_date
        ),
        None,
    )
    if not occurrence:
        raise HTTPException(400, "Выбранного занятия нет в расписании этой группы")
    if require_current_or_future:
        # #88: an exam is never bound to a lesson the dialog would not offer (completed, cancelled, or over).
        status_of_lesson = await _lesson_attendance_status(
            actor, group_id=str(group["id"]), lesson_id=lesson_id, lesson_date=lesson_date
        )
        now = _business_now()  # one reading: the date and the minutes must come from the same instant
        if not _lesson_is_bindable(
            lesson_date=lesson_date,
            end_time=occurrence.get("endTime"),
            lesson_status=status_of_lesson,
            today=now.date(),
            now_minutes=now.hour * 60 + now.minute,
        ):
            raise HTTPException(409, LESSON_OVER_MESSAGE)
    return group, occurrence


def _ensure_lesson_slot_is_available(
    db: Session,
    exam: GeneratedExam,
    *,
    org_id: str,
    lesson_id: str,
    lesson_date: str,
) -> None:
    existing = (
        db.query(GeneratedExam)
        .filter(
            GeneratedExam.org_id == org_id,
            GeneratedExam.scheduled_lesson_id == lesson_id,
            GeneratedExam.scheduled_lesson_date == lesson_date,
            GeneratedExam.status.in_(["lobby", "print_ready", "active"]),
            GeneratedExam.id != exam.id,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            409,
            f"На это занятие уже назначен экзамен «{existing.title}». Сначала перенесите или завершите его.",
        )


async def _require_lesson_in_progress(exam: GeneratedExam, actor: ExamActor) -> None:
    """An exam starts only after its exact lesson has been started."""
    _require_bound_exam(exam)
    assert exam.group_id and exam.scheduled_lesson_id and exam.scheduled_lesson_date
    if exam.scheduled_lesson_date != _business_today().isoformat():
        raise HTTPException(409, "Экзамен можно начать только в день назначенного занятия")

    await _resolve_lesson_occurrence(
        actor,
        group_id=str(exam.group_id),
        lesson_id=exam.scheduled_lesson_id,
        lesson_date=exam.scheduled_lesson_date,
        require_current_or_future=False,
    )
    url = (
        f"{get_settings().auth_service_url.rstrip('/')}/api/v1/auth/org/groups/"
        f"{exam.group_id}/attendance"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                url,
                params={
                    "date_from": exam.scheduled_lesson_date,
                    "date_to": exam.scheduled_lesson_date,
                },
                headers=dict(actor.auth_headers),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Не удалось проверить статус урока. Повторите попытку.") from exc
    if response.status_code != 200:
        raise HTTPException(503, "Не удалось проверить статус урока")
    attendance = response.json()
    lesson_key = f"{exam.scheduled_lesson_date}_{exam.scheduled_lesson_id}"
    if (attendance.get("statuses") or {}).get(lesson_key) != "in_progress":
        raise HTTPException(409, "Сначала начните назначенный урок, затем запускайте экзамен")


async def _require_public_lobby_available(exam: GeneratedExam) -> None:
    """Expose an online lobby only during its teacher-started lesson.

    The public exam URL is intentionally not a scheduling authority.  Until
    the teacher has explicitly started the exact lesson occurrence, students
    cannot create waiting sessions or reserve a variant.  The check is made
    server-side for every public lobby operation, so a copied URL cannot
    bypass the teacher workflow.
    """
    _require_bound_exam(exam)
    assert exam.group_id and exam.scheduled_lesson_id and exam.scheduled_lesson_date

    try:
        lesson_date = date.fromisoformat(exam.scheduled_lesson_date)
    except ValueError as exc:  # Defensive guard for legacy rows.
        raise HTTPException(410, "У экзамена указана некорректная дата занятия") from exc

    today = _business_today()
    if lesson_date > today:
        raise HTTPException(409, "Экзамен станет доступен после начала назначенного урока")
    if lesson_date < today:
        raise HTTPException(410, "Назначенное занятие уже прошло. Обратитесь к преподавателю.")

    service_token = get_settings().internal_service_token
    if not service_token:
        log.critical("Public lobby guard rejected: INTERNAL_SERVICE_TOKEN is not configured")
        raise HTTPException(503, "Не удалось проверить статус назначенного урока")

    url = (
        f"{get_settings().auth_service_url.rstrip('/')}/api/v1/internal/org/{exam.org_id}"
        f"/groups/{exam.group_id}/lessons/{exam.scheduled_lesson_id}/attendance"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                url,
                params={"lesson_date": exam.scheduled_lesson_date},
                headers={"X-Internal-Service-Token": service_token},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Не удалось проверить статус назначенного урока") from exc

    if response.status_code != 200:
        log.warning(
            "Public lobby guard failed for exam %s: auth-service returned %s",
            exam.id,
            response.status_code,
        )
        raise HTTPException(503, "Не удалось проверить статус назначенного урока")

    lesson_status = response.json().get("status")
    if lesson_status == "cancelled":
        raise HTTPException(410, "Назначенный урок отменён. Экзамен недоступен.")
    if lesson_status != "in_progress":
        raise HTTPException(409, "Экзамен станет доступен после начала урока преподавателем")


def _count_answered(answers: Any) -> int:
    """Answers the student really gave: a cleared field keeps its key in the map with an empty string."""
    if not isinstance(answers, dict):
        return 0
    return sum(1 for value in answers.values() if value is not None and str(value).strip())


class CodedHTTPException(HTTPException):
    """An expected refusal that also carries a stable machine code (the body gets ``{"detail": text, "code": CODE}``).

    ``detail`` stays a plain string, so every existing client keeps showing the server's text.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code, message)
        self.code = code


def _tokens_match(actual: str, supplied: str) -> bool:
    """Constant-time token comparison that never raises on a header value.

    ``secrets.compare_digest(str, str)`` raises ``TypeError`` for a non-ASCII string, and Starlette hands headers over
    decoded as latin-1, so ``X-Exam-Session-Token: é`` used to end in a 500.  Comparing UTF-8 bytes keeps the
    comparison constant-time and turns any such value into a plain mismatch (403 at the caller).
    """
    try:
        return secrets.compare_digest(str(actual).encode("utf-8"), str(supplied).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        return False


def _require_internal_service_token(token: Optional[str]) -> None:
    expected = get_settings().internal_service_token
    if not expected:
        log.critical("Internal lesson lifecycle call rejected: INTERNAL_SERVICE_TOKEN is not configured")
        raise HTTPException(503, "Внутренняя связка сервисов не настроена")
    if not token or not _tokens_match(token, expected):
        raise HTTPException(403, "Недействительный внутренний токен сервиса")


def _lesson_cancellation_blocker(
    db: Session,
    *,
    org_id: str,
    group_id: str,
    lesson_id: str,
    lesson_date: str,
    lock: bool = False,
) -> tuple[list[GeneratedExam], list[GeneratedExam]]:
    query = db.query(GeneratedExam).filter(
        GeneratedExam.org_id == org_id,
        GeneratedExam.group_id == group_id,
        GeneratedExam.scheduled_lesson_id == lesson_id,
        GeneratedExam.scheduled_lesson_date == lesson_date,
        GeneratedExam.status.in_(["lobby", "print_ready", "active"]),
    )
    if lock:
        query = query.with_for_update()
    exams = query.all()
    blocked = [exam for exam in exams if exam.status == "active" or _has_any_sessions(db, exam.id)]
    return exams, blocked


def _recurring_end_exam_impact(
    db: Session,
    *,
    org_id: str,
    group_id: str,
    lesson_id: str,
    effective_from: str,
    lock: bool = False,
) -> tuple[list[GeneratedExam], list[dict[str, Any]], list[GeneratedExam]]:
    """Return every still-operational exam affected by ending a recurrence.

    Only open delivery states are included. Closed/archived exams are durable
    historical facts and must not be rewritten merely because a future
    schedule rule is stopped.
    """
    query = db.query(GeneratedExam).filter(
        GeneratedExam.org_id == org_id,
        GeneratedExam.group_id == group_id,
        GeneratedExam.scheduled_lesson_id == lesson_id,
        GeneratedExam.scheduled_lesson_date >= effective_from,
        GeneratedExam.status.in_(["lobby", "print_ready", "active"]),
    )
    if lock:
        query = query.with_for_update()
    exams = query.order_by(GeneratedExam.scheduled_lesson_date, GeneratedExam.created_at).all()
    details: list[dict[str, Any]] = []
    blocked: list[GeneratedExam] = []
    for exam in exams:
        has_sessions = _has_any_sessions(db, exam.id)
        is_blocked = exam.status == "active" or has_sessions
        if is_blocked:
            blocked.append(exam)
        details.append(
            {
                "exam_id": exam.id,
                "title": exam.title,
                "lesson_date": exam.scheduled_lesson_date,
                "delivery_mode": exam.delivery_mode or "online",
                "status": exam.status,
                "can_release": not is_blocked,
                "blocker_reason": (
                    "Экзамен уже идёт"
                    if exam.status == "active"
                    else "У учеников уже есть попытки"
                    if has_sessions
                    else None
                ),
            }
        )
    return exams, details, blocked


STUDENT_NOT_FOUND_MESSAGE = "Ученик не найден в этой группе — проверьте имя или ID"


async def _canonical_student_for_exam(
    exam: GeneratedExam,
    *,
    student_id: Optional[str],
    student_name: Optional[str],
    identifier: Optional[str] = None,
) -> dict:
    """Resolve one roster member for a public exam entry, failing closed.

    The browser pre-check is a convenience only.  ``start`` calls this helper
    again and uses its canonical ID/name, so a caller cannot bypass the group
    boundary or forge another student's identity with a direct API request.
    """
    if not exam.group_id:
        raise HTTPException(409, "Экзамен не привязан к группе и не может быть начат")

    params: dict[str, str] = {}
    if student_id:
        params["student_id"] = student_id.strip()
    if student_name:
        params["student_name"] = student_name.strip()
    if identifier:
        params["q"] = identifier.strip()
    if not params:
        raise HTTPException(400, "Укажите идентификатор или ФИО ученика")

    url = (
        f"{get_settings().auth_service_url.rstrip('/')}/api/v1/auth/public/groups/"
        f"{exam.group_id}/verify-student"
    )

    async def ask(extra: Optional[dict[str, str]] = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    url,
                    params={**params, **(extra or {})},
                    headers={"X-Internal-Service-Token": get_settings().internal_service_token},
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                503,
                "Не удалось проверить принадлежность к группе. Повторите попытку.",
            ) from exc
        if response.status_code != 200:
            raise HTTPException(503, "Не удалось проверить принадлежность к группе")
        return response.json()

    def enrolled(data: dict) -> bool:
        student = data.get("student")
        return bool(data.get("enrolled")) and isinstance(student, dict) and bool(student.get("id"))

    data = await ask()
    if not enrolled(data):
        # LOGIC-B O17: an exam that is RUNNING stays reachable for a student who was in the group when it began and was moved / archived
        # afterwards - but only to RESUME the attempt he already has (the caller checks that: ``_resume_only``).  Nobody who was not
        # enrolled at the common start of the exam gets in.
        started_at = _exam_started_at_of_snapshot(exam)
        if getattr(exam, "status", None) == "active" and started_at is not None:
            retry = await ask({"as_of": started_at.isoformat()})
            if enrolled(retry):
                return {**retry["student"], "_resume_only": True}
        # One answer for every refusal (unknown ID, wrong name for an ID, a name shared by two students, an empty
        # group): the text of auth-service's reason tells an outsider whether an ID exists or a name is taken.
        raise HTTPException(403, STUDENT_NOT_FOUND_MESSAGE)
    return data["student"]


def _exam_started_at_of_snapshot(exam: GeneratedExam) -> Optional[datetime]:
    """``settings.started_at`` of a detached exam copy as naive UTC (no database access: the entry runs without a transaction)."""
    raw = (getattr(exam, "settings", None) or {}).get("started_at")
    if not raw:
        return None
    try:
        return _to_naive_utc(datetime.fromisoformat(raw))
    except (TypeError, ValueError):
        return None


def _has_resumable_session(db: Session, exam_id: str, student: dict) -> bool:
    """O17: the student has an attempt IN PROGRESS in this exam (the only thing a student who left the group may come back to)."""
    sessions = SessionRepository(db).list_by_exam(exam_id)
    found = _find_student_session(sessions, ("in_progress",), student.get("id"), student.get("display_id"))
    db.commit()  # ends the read transaction
    return found is not None


def _require_session_capability(session: ExamSession, session_token: Optional[str]) -> None:
    """Validate the attempt capability without leaking whether it exists."""
    if not session_token or not session.session_token:
        raise HTTPException(403, "Нет доступа к попытке экзамена")
    if not _tokens_match(session.session_token, session_token):
        raise HTTPException(403, "Нет доступа к попытке экзамена")


def _tasks_for_session(exam: GeneratedExam, session: ExamSession) -> list[dict]:
    variants = exam.variants or []
    variant = next(
        (item for item in variants if item.get("variant_number") == session.variant_number),
        variants[0] if variants else {},
    )
    return list((variant or {}).get("tasks", []))


def _validate_answer_keys(exam: GeneratedExam, session: ExamSession, answers: Dict[str, str]) -> None:
    known_task_ids = {str(task.get("task_id")) for task in _tasks_for_session(exam, session)}
    unknown_task_ids = set(answers) - known_task_ids
    if unknown_task_ids:
        raise HTTPException(422, "Передан ответ для задания, которого нет в варианте")


def _difficulty_counts(tasks_count: int, dist: Dict[str, float]) -> Dict[str, int]:
    """Convert requested percentages into the closest possible A/B/C counts.

    Task counts are integers, so a percentage split cannot always be exact.
    We use the largest-remainder method: take the whole-number quota first and
    distribute the remaining tasks to the biggest fractional remainders.  The
    result always sums to ``tasks_count`` and is as close as possible to the
    teacher's chosen split.
    """
    keys = ("easy", "medium", "hard")
    try:
        requested = {key: float(dist.get(key, 0)) for key in keys}
    except (TypeError, ValueError):
        raise HTTPException(422, "Распределение сложности должно содержать числа")

    if any(not math.isfinite(value) or value < 0 for value in requested.values()):
        raise HTTPException(422, "Доли сложности должны быть неотрицательными числами")

    total_pct = sum(requested.values())
    if total_pct <= 0:
        raise HTTPException(422, "Укажите хотя бы одну ненулевую долю сложности")

    exact = {key: tasks_count * requested[key] / total_pct for key in keys}
    counts = {key: math.floor(exact[key]) for key in keys}
    remainder = tasks_count - sum(counts.values())
    # Stable key order resolves equal fractions without a hidden random bias.
    for key in sorted(keys, key=lambda item: (- (exact[item] - counts[item]), keys.index(item)))[:remainder]:
        counts[key] += 1

    return {"A": counts["easy"], "B": counts["medium"], "C": counts["hard"]}


def _should_show_mc(task: dict, mc_mode: str = "auto") -> bool:
    return should_show_mc(task, mc_mode)



def _build_mc_options(
    task: dict,
    *,
    shuffle: bool,
    seed: Optional[str] = None,
) -> List[str]:
    """Build an MCQ list with a stable order for one student attempt.

    A browser can re-fetch an active exam after a reconnect.  Using a global
    random shuffle there makes letters jump between requests.  A deterministic
    session seed gives every online student an independent order without ever
    changing it within their attempt.  Options that are bare letters (they
    point at choices printed in the question) keep their order.
    """
    pairs = _mc_choices(task)
    if shuffle:
        pairs = order_choices(pairs, seed if seed is not None else secrets.token_hex(8))
    return [value for value, _ in pairs]


def _mc_choices(task: dict) -> List[tuple[str, str]]:
    """(value, display) choices; the key once, graded by this service's grader."""
    return mc_choices(task)


def _build_session_mc_options(exam: GeneratedExam, session: ExamSession, task: dict) -> List[str]:
    """Return option order fixed to one student session."""
    shuffle_options = bool((exam.settings or {}).get("shuffle_options", True))
    task_id = str(task.get("task_id") or task.get("id") or "")
    return _build_mc_options(
        task,
        shuffle=shuffle_options,
        seed=f"{exam.id}:{session.id}:{task_id}",
    )


def _build_mc_options_latex(task: dict, options: List[str]) -> List[str]:
    """Parallel LaTeX strings for MCQ options (same order as options)."""
    if not options:
        return []
    # Values are unique among the choices, so each display is found exactly.
    display_by_value = dict(_mc_choices(task))
    return [display_by_value.get(opt, "") for opt in options]


from src.domain.answer_utils import normalise_answer as _normalise_answer, check_answer as _check_answer
from src.domain.distractor_utils import (
    build_analytics_hit,
    match_distractor as _match_distractor,
    normalize_distractor_meta,
    normalize_answer_options,
    resolve_analytics_explanation,
)


def _build_task_pool(
    req: GenerateExamRequest,
    task_repo: TaskRepository,
) -> List[dict]:
    """Select tasks for the exam draft — Mode A (skills) or Mode B (textbook)."""
    dist = req.difficulty_distribution or {"easy": 30, "medium": 50, "hard": 20}
    counts = _difficulty_counts(req.tasks_count, dist)

    all_tasks: list = []

    if req.skill_ids:
        # Mode A: by curriculum skills
        used: List[str] = []
        for diff, cnt in counts.items():
            if cnt <= 0:
                continue
            rows = task_repo.get_tasks_by_skills(req.skill_ids, diff, cnt, used)
            all_tasks.extend(rows)
            used.extend(r.get("task_id") or r.get("id", "") for r in rows)
        # Fill remainder if not enough per difficulty
        if len(all_tasks) < req.tasks_count:
            fill = task_repo.get_tasks_by_skills(
                req.skill_ids, None,
                req.tasks_count - len(all_tasks),
                [t.get("task_id") or t.get("id", "") for t in all_tasks],
            )
            all_tasks.extend(fill)

    else:
        # Mode B: by textbook toc
        toc_ids_expanded = task_repo.expand_toc_ids(req.paragraph_ids)
        if not toc_ids_expanded:
            raise NoTasksAvailableError("Параграфы не найдены в textbook_toc")
        used = []
        for diff, cnt in counts.items():
            if cnt <= 0:
                continue
            rows = task_repo.get_tasks_by_toc_ids(toc_ids_expanded, diff, cnt, used)
            all_tasks.extend(rows)
            used.extend(r.get("task_id") or r.get("id", "") for r in rows)
        if len(all_tasks) < req.tasks_count:
            fill = task_repo.get_tasks_by_toc_ids(
                toc_ids_expanded, None,
                req.tasks_count - len(all_tasks),
                [t.get("task_id") or t.get("id", "") for t in all_tasks],
            )
            all_tasks.extend(fill)

    return all_tasks[: req.tasks_count]


def _normalize_content_task(task: dict) -> dict:
    """Content API returns ``id``; exam snapshots use ``task_id``."""
    out = dict(task)
    if not out.get("task_id"):
        out["task_id"] = out.get("id", "")
    return out


def _order_tasks_by_section(tasks: List[dict]) -> List[dict]:
    """Keep the exam in section blocks instead of a random difficulty order.

    The draft and printed package are both organised around visible skill
    sections.  The online path must use that same grouping, while preserving
    the task order within each section selected by the generator.
    """
    sections: Dict[str, List[dict]] = {}
    for task in tasks:
        key = str(task.get("skill_name") or task.get("skill_id") or "Без категории").strip()
        sections.setdefault(key or "Без категории", []).append(task)
    return [task for section in sections.values() for task in section]


# ═══════════════════════════════════════════════════════════════════════════
# TEACHER endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/exams/lesson-binding", summary="Связанный экзамен конкретного занятия")
def get_lesson_exam_binding(
    group_id: str = Query(..., min_length=1),
    lesson_id: str = Query(..., min_length=1),
    lesson_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Return cancellation-safe operational state for a schedule modal."""
    _await(require_group_access(actor, group_id), db)
    exams, blocked = _lesson_cancellation_blocker(
        db,
        org_id=actor.org_id,
        group_id=group_id,
        lesson_id=lesson_id,
        lesson_date=lesson_date,
    )
    exam = exams[0] if exams else None
    if not exam:
        return {"attached": False, "can_cancel": True, "exam": None, "message": None}
    if blocked:
        return {
            "attached": True,
            "can_cancel": False,
            "exam": {"id": exam.id, "title": exam.title, "status": exam.status},
            "message": "Сначала завершите активный экзамен или дождитесь окончания попыток учеников.",
        }
    return {
        "attached": True,
        "can_cancel": True,
        "exam": {"id": exam.id, "title": exam.title, "status": exam.status},
        "message": "Экзамен вернётся в черновик, а код доступа будет закрыт.",
    }


@router.post("/internal/lesson-bindings/cancellation-check", summary="Internal: проверить отмену урока")
def check_lesson_cancellation(
    req: LessonCancellationSyncRequest,
    x_internal_service_token: Optional[str] = Header(None, alias="X-Internal-Service-Token"),
    db: Session = Depends(get_db),
):
    """Fail closed before auth-service persists a lesson cancellation."""
    _require_internal_service_token(x_internal_service_token)
    _, blocked = _lesson_cancellation_blocker(
        db,
        org_id=req.org_id,
        group_id=req.group_id,
        lesson_id=req.lesson_id,
        lesson_date=req.lesson_date,
    )
    if blocked:
        names = ", ".join(f"«{exam.title}»" for exam in blocked[:2])
        raise HTTPException(
            409,
            f"Нельзя отменить урок: экзамен {names} уже запущен или имеет попытки учеников.",
        )
    return {"ok": True, "can_cancel": True}


@router.post("/internal/lesson-bindings/release", summary="Internal: освободить экзамен при отмене урока")
def release_lesson_exam_binding(
    req: LessonCancellationSyncRequest,
    x_internal_service_token: Optional[str] = Header(None, alias="X-Internal-Service-Token"),
    db: Session = Depends(get_db),
):
    """Return an untouched lobby to draft after its lesson is cancelled.

    The row lock plus the same predicate as the public unique index makes this
    idempotent and protects against a student joining between preflight and
    cancellation.  Active or attempted exams are deliberately refused.
    """
    _require_internal_service_token(x_internal_service_token)
    exams, blocked = _lesson_cancellation_blocker(
        db,
        org_id=req.org_id,
        group_id=req.group_id,
        lesson_id=req.lesson_id,
        lesson_date=req.lesson_date,
        lock=True,
    )
    if blocked:
        db.rollback()
        names = ", ".join(f"«{exam.title}»" for exam in blocked[:2])
        raise HTTPException(409, f"Экзамен {names} уже нельзя безопасно вернуть в черновик")

    released: list[str] = []
    for exam in exams:
        if exam.delivery_mode == "print":
            _void_print_package_for_cancelled_lesson(
                db,
                exam,
                reason=req.reason,
                actor_id="schedule-service",
            )
        else:
            _reset_lobby_to_draft(
                db,
                exam,
                lifecycle_event={
                    "type": "lesson_cancelled",
                    "lesson_id": req.lesson_id,
                    "lesson_date": req.lesson_date,
                    "group_id": req.group_id,
                    "group_name": exam.group_name,
                    "reason": req.reason,
                },
            )
        released.append(exam.id)
    db.commit()
    return {"ok": True, "released_exam_ids": released}


def _exam_remaining_seconds(db: Session, exam: GeneratedExam) -> Optional[int]:
    """Seconds left on the common timer of an ACTIVE exam (UTC); ``None`` when it has not started."""
    started_at = _exam_started_at(db, exam)
    if exam.status != "active" or not started_at:
        return None
    total_seconds = (exam.total_time_minutes or 45) * 60
    return max(0, total_seconds - max(0, int((datetime.utcnow() - started_at).total_seconds())))


def _waiting_session_count(db: Session, exam_id: str) -> int:
    return db.query(ExamSession.id).filter(ExamSession.exam_id == exam_id, ExamSession.status == "waiting").count()


def _has_started_attempt(db: Session, exam_id: str) -> bool:
    """An attempt that is not just a name in the lobby: it has (or may have) answers and is part of the history."""
    return db.query(ExamSession.id).filter(ExamSession.exam_id == exam_id, ExamSession.status != "waiting").first() is not None


def _release_untouched_exam(db: Session, exam: GeneratedExam, *, event_type: str, reason: str, actor_id: Optional[str], lesson_event: dict) -> None:
    """Return a lobby / print-ready exam without started attempts to a reusable draft; the people waiting in its lobby are removed
    (they have no answers)."""
    db.query(ExamSession).filter(ExamSession.exam_id == exam.id, ExamSession.status == "waiting").delete(synchronize_session=False)
    if exam.delivery_mode == "print":
        package = db.query(ExamPrintPackage).filter(ExamPrintPackage.exam_id == exam.id).first()
        if package:
            package.status = "voided"
    _reset_lobby_to_draft(
        db,
        exam,
        actor_id=actor_id or "schedule-service",
        lifecycle_event={"type": event_type, "reason": reason, **lesson_event},
    )


@router.post("/internal/lesson-bindings/completion-check", summary="Internal: экзамены урока перед его завершением")
def check_lesson_completion(
    req: LessonCompletionSyncRequest,
    x_internal_service_token: Optional[str] = Header(None, alias="X-Internal-Service-Token"),
    db: Session = Depends(get_db),
):
    """What is still live when the teacher ends the lesson: an exam that runs on its own timer, or one that has not started.

    Read-only.  An exam whose time has already run out does not count (the first request after the deadline closes it).
    """
    _require_internal_service_token(x_internal_service_token)
    exams = (
        db.query(GeneratedExam)
        .filter(
            GeneratedExam.org_id == req.org_id,
            GeneratedExam.group_id == req.group_id,
            GeneratedExam.scheduled_lesson_id == req.lesson_id,
            GeneratedExam.scheduled_lesson_date == req.lesson_date,
            GeneratedExam.status.in_(["lobby", "print_ready", "active"]),
        )
        .order_by(GeneratedExam.created_at)
        .all()
    )
    items = []
    for exam in exams:
        remaining = _exam_remaining_seconds(db, exam)
        if exam.status == "active" and remaining is not None and remaining <= 0:
            continue
        started_at = _exam_started_at(db, exam) if exam.status == "active" else None
        ends_at = (started_at + timedelta(minutes=exam.total_time_minutes or 45)).isoformat() if started_at else None
        items.append(
            {
                "examId": exam.id,
                "title": exam.title,
                "status": exam.status,
                "deliveryMode": exam.delivery_mode or "online",
                "remainingSeconds": remaining,
                "waitingCount": _waiting_session_count(db, exam.id),
                "endsAt": ends_at,
            }
        )
    db.commit()  # ends the read transaction
    return {"ok": True, "exams": items}


@router.post("/internal/lesson-bindings/completed", summary="Internal: урок завершён - освободить нетронутые экзамены")
def lesson_completed(
    req: LessonCompletionSyncRequest,
    x_internal_service_token: Optional[str] = Header(None, alias="X-Internal-Service-Token"),
    db: Session = Depends(get_db),
):
    """The lesson is over.  An ACTIVE exam is left alone: it runs by its own timer and closes itself.  A lobby / print-ready exam without
    started attempts can never start now (starting needs a lesson in progress): it goes back to a draft (event ``lesson_completed``) and
    its waiting sessions are deleted.  The exams are locked and re-read, so a student who entered the lobby a moment ago is judged on
    the state at this very instant.  Idempotent."""
    _require_internal_service_token(x_internal_service_token)
    exams = (
        db.query(GeneratedExam)
        .filter(
            GeneratedExam.org_id == req.org_id,
            GeneratedExam.group_id == req.group_id,
            GeneratedExam.scheduled_lesson_id == req.lesson_id,
            GeneratedExam.scheduled_lesson_date == req.lesson_date,
            GeneratedExam.status.in_(["lobby", "print_ready", "active"]),
        )
        .order_by(GeneratedExam.id)
        .with_for_update()
        .populate_existing()
        .all()
    )
    released: list[str] = []
    left_running: list[str] = []
    for exam in exams:
        if exam.status == "active":
            remaining = _exam_remaining_seconds(db, exam)
            if remaining is None or remaining > 0:
                left_running.append(exam.id)
            continue
        if _has_started_attempt(db, exam.id):
            left_running.append(exam.id)
            continue
        _release_untouched_exam(
            db, exam, event_type="lesson_completed", reason="Урок завершён", actor_id=req.actor_id,
            lesson_event={"group_id": req.group_id, "group_name": exam.group_name, "lesson_id": req.lesson_id, "lesson_date": req.lesson_date},
        )
        released.append(exam.id)
    db.commit()
    return {"ok": True, "released": released, "leftRunning": left_running}


def _group_exams(db: Session, org_id: str, group_id: str, *, lock: bool = False) -> list[GeneratedExam]:
    query = db.query(GeneratedExam).filter(
        GeneratedExam.org_id == org_id,
        GeneratedExam.group_id == group_id,
        GeneratedExam.status.in_(["lobby", "print_ready", "active"]),
    )
    if lock:
        query = query.order_by(GeneratedExam.id).with_for_update().populate_existing()
    return query.all()


def _archive_blocker_reason(db: Session, exam: GeneratedExam) -> Optional[str]:
    if exam.status == "active":
        return "Экзамен уже идёт"
    if _has_started_attempt(db, exam.id):
        return "У учеников уже есть попытки"
    return None


@router.post("/internal/group-bindings/archive-impact", summary="Internal: экзамены группы перед архивацией")
def group_archive_impact(
    req: GroupBindingSyncRequest,
    x_internal_service_token: Optional[str] = Header(None, alias="X-Internal-Service-Token"),
    db: Session = Depends(get_db),
):
    """Every operational exam of the group and whether it can be released (back to a draft)."""
    _require_internal_service_token(x_internal_service_token)
    items = []
    for exam in sorted(_group_exams(db, req.org_id, req.group_id), key=lambda e: (e.scheduled_lesson_date or "", e.id)):
        reason = _archive_blocker_reason(db, exam)
        items.append(
            {
                "examId": exam.id,
                "title": exam.title,
                "status": exam.status,
                "lessonDate": exam.scheduled_lesson_date,
                "canRelease": reason is None,
                "blockerReason": reason,
            }
        )
    db.commit()  # ends the read transaction
    return {"exams": items}


@router.post("/internal/group-bindings/archive-release", summary="Internal: освободить экзамены архивируемой группы (всё или ничего)")
def group_archive_release(
    req: GroupBindingSyncRequest,
    x_internal_service_token: Optional[str] = Header(None, alias="X-Internal-Service-Token"),
    db: Session = Depends(get_db),
):
    """All or nothing: if any exam of the group is active or has started attempts (e.g. it started after the preview) the whole call is
    refused (409) and nothing changes; otherwise every exam goes back to a draft and the group's lobby sessions are removed."""
    _require_internal_service_token(x_internal_service_token)
    exams = _group_exams(db, req.org_id, req.group_id, lock=True)
    blocked = [exam for exam in exams if _archive_blocker_reason(db, exam)]
    if blocked:
        db.commit()  # nothing was changed: only ends the transaction and drops the row locks
        names = ", ".join(f"«{exam.title}»" for exam in blocked[:2])
        raise HTTPException(409, f"Нельзя архивировать группу: экзамен {names} уже идёт или имеет попытки учеников")
    released: list[str] = []
    for exam in exams:
        _release_untouched_exam(
            db, exam, event_type="group_archived", reason=req.reason or "Группа архивирована", actor_id=req.actor_id,
            lesson_event={"group_id": req.group_id, "group_name": exam.group_name, "lesson_id": exam.scheduled_lesson_id,
                          "lesson_date": exam.scheduled_lesson_date},
        )
        released.append(exam.id)
    db.commit()
    return {"ok": True, "releasedExamIds": released}


@router.post(
    "/internal/lesson-bindings/recurring-end-impact",
    summary="Internal: последствия прекращения регулярных занятий",
)
def get_recurring_lesson_end_impact(
    req: RecurringLessonEndSyncRequest,
    x_internal_service_token: Optional[str] = Header(None, alias="X-Internal-Service-Token"),
    db: Session = Depends(get_db),
):
    """Preview future delivery consequences without mutating any exam."""
    _require_internal_service_token(x_internal_service_token)
    _, exams, blocked = _recurring_end_exam_impact(
        db,
        org_id=req.org_id,
        group_id=req.group_id,
        lesson_id=req.lesson_id,
        effective_from=req.effective_from,
    )
    return {
        "can_end": not blocked,
        "affected_exam_count": len(exams),
        "releasable_exam_count": len(exams) - len(blocked),
        "blocked_exam_count": len(blocked),
        "exams": exams,
    }


@router.post(
    "/internal/lesson-bindings/recurring-end-release",
    summary="Internal: освободить будущие экзамены при прекращении регулярности",
)
def release_recurring_lesson_exams(
    req: RecurringLessonEndSyncRequest,
    x_internal_service_token: Optional[str] = Header(None, alias="X-Internal-Service-Token"),
    db: Session = Depends(get_db),
):
    """Atomically return every confirmed untouched future delivery to draft.

    The query is locked and repeats the session check performed by the
    preview. A student may safely join between UI preview and final click:
    this endpoint then refuses the entire operation instead of releasing a
    partially changed set of future exams.
    """
    _require_internal_service_token(x_internal_service_token)
    exams, _, blocked = _recurring_end_exam_impact(
        db,
        org_id=req.org_id,
        group_id=req.group_id,
        lesson_id=req.lesson_id,
        effective_from=req.effective_from,
        lock=True,
    )
    if blocked:
        db.rollback()
        names = ", ".join(f"«{exam.title}»" for exam in blocked[:2])
        raise HTTPException(
            409,
            f"Нельзя прекратить регулярные занятия: экзамен {names} уже активен или имеет попытки учеников.",
        )

    released: list[str] = []
    for exam in exams:
        if exam.delivery_mode == "print" or exam.status == "print_ready":
            _void_print_package_for_cancelled_lesson(
                db,
                exam,
                reason=req.reason,
                actor_id=req.actor_id or "schedule-service",
                disposition="recurring_schedule_ended",
            )
        else:
            _reset_lobby_to_draft(
                db,
                exam,
                actor_id=req.actor_id or "schedule-service",
                lifecycle_event={
                    "type": "recurring_schedule_ended",
                    "group_id": req.group_id,
                    "group_name": req.group_name or exam.group_name,
                    "lesson_id": req.lesson_id,
                    "lesson_date": exam.scheduled_lesson_date,
                    "reason": req.reason,
                    "details": {"effective_from": req.effective_from},
                },
            )
        released.append(exam.id)
    db.commit()
    return {"ok": True, "released_exam_ids": released}


@router.post("/internal/lesson-bindings/rescheduled", summary="Internal: зафиксировать перенос занятия")
def record_lesson_reschedule(
    req: LessonRescheduleSyncRequest,
    x_internal_service_token: Optional[str] = Header(None, alias="X-Internal-Service-Token"),
    db: Session = Depends(get_db),
):
    """Record a one-off reschedule without changing the exam binding.

    An exam binds to the stable occurrence identity (lesson id + date), so a
    transfer naturally follows the lesson.  This endpoint records the old and
    new schedule snapshots for later administrator history; it never opens,
    starts, closes or reassigns an exam.
    """
    _require_internal_service_token(x_internal_service_token)
    exams = (
        db.query(GeneratedExam)
        .filter(
            GeneratedExam.org_id == req.org_id,
            GeneratedExam.group_id == req.group_id,
            GeneratedExam.scheduled_lesson_id == req.lesson_id,
            GeneratedExam.scheduled_lesson_date == req.lesson_date,
            GeneratedExam.status.in_(["lobby", "print_ready", "active"]),
        )
        .with_for_update()
        .all()
    )
    for exam in exams:
        _record_exam_lifecycle_event(
            db,
            exam,
            {
                "type": "lesson_rescheduled",
                "group_id": req.group_id,
                "group_name": req.group_name or exam.group_name,
                "lesson_id": req.lesson_id,
                "lesson_date": req.lesson_date,
                "reason": req.reason,
                "details": {
                    "previous": req.previous,
                    "current": req.current,
                    "source": "schedule_occurrence",
                },
            },
            actor_id=req.actor_id,
        )
    db.commit()
    return {"ok": True, "recorded_exam_ids": [exam.id for exam in exams]}


@router.post("/exams/generate", summary="Создать черновик экзамена")
def generate_exam(
    req: GenerateExamRequest,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    _require_teacher_exam_operator(actor)
    if req.delivery_mode == "hybrid":
        # Hybrid requires a separate answer-capture and reconciliation flow;
        # accepting it before that exists would create an unfinishable exam.
        raise HTTPException(422, "Гибридный формат будет доступен после настройки ввода бумажных ответов")
    if not req.skill_ids and not req.paragraph_ids:
        raise HTTPException(400, "Укажите skill_ids или paragraph_ids")

    task_repo = TaskRepository()
    exam_repo = ExamRepository(db)

    tasks = _order_tasks_by_section(
        [_normalize_content_task(t) for t in _build_task_pool(req, task_repo)]
    )
    if not tasks:
        raise HTTPException(
            400,
            "Не найдено задач. Проверьте, что задачи оцифрованы и активны.",
        )

    source = "topics" if req.skill_ids else "textbook"
    exam_id = _new_exam_id()
    skills_set = {t["skill_id"] for t in tasks if t["skill_id"]}

    group = _await(require_group_access(actor, req.group_id), db) if req.group_id else None
    exam = GeneratedExam(
        id=exam_id,
        title=req.title,
        teacher_name=req.teacher_name,
        org_id=actor.org_id,
        created_by=actor.user_id,
        group_id=str(group["id"]) if group else None,
        group_name=group.get("name") if group else None,
        class_level=group.get("class_level") if group else req.class_level,
        source_mode=source,
        delivery_mode=req.delivery_mode,
        variants=[{"variant_number": 1, "tasks": tasks}],
        settings={
            "time_minutes": req.time_minutes,
            "shuffle_options": bool(req.shuffle_options) if req.delivery_mode == "online" else False,
            "mc_mode": req.mc_mode,
            "difficulty_distribution": req.difficulty_distribution,
            "skill_ids": req.skill_ids,
            "paragraph_ids": req.paragraph_ids,
        },
        skills_covered=list(skills_set),
        toc_ids_used=req.paragraph_ids,
        status="draft",
        total_time_minutes=req.time_minutes,
        variants_count=1,
        tasks_per_variant=len(tasks),
    )

    # Optional: auto-generate variants via VariantOrchestrator
    if req.generate_variants and req.variants_count > 1:
        try:
            orch = VariantOrchestrator(db=db)
            report = orch.generate_variants(tasks, req.variants_count, seed=42)
            exam.variants = [v.to_dict() for v in report.variants]
            exam.variants_count = report.total_variants
            exam.status = "variants_ready"
        except Exception as exc:
            log.warning("Variant generation failed, keeping single variant: %s", exc)

    if req.delivery_mode == "print":
        # This remains editable while the exam is a draft.  The exact
        # snapshot is written only by the explicit prepare-package action.
        settings = dict(exam.settings or {})
        settings["print_layout"] = build_default_print_layout(exam.variants or [])
        exam.settings = settings

    # The lifecycle history has a database foreign key to generated_exams.
    # Flush the parent first, while keeping both writes in one transaction.
    _add_new_exam(db, exam)
    exam_id = exam.id  # an id collision may have drawn a new one
    _record_exam_lifecycle_event(
        db,
        exam,
        {
            "type": "exam_created",
            "group_id": exam.group_id,
            "group_name": exam.group_name,
            "details": {
                "initial_status": exam.status,
                "source_mode": source,
                "delivery_mode": req.delivery_mode,
            },
        },
        actor_id=actor.user_id,
    )
    db.commit()
    db.refresh(exam)
    log.info("Exam created: %s, %d tasks, group=%s, source=%s", exam_id, len(tasks), req.group_name, source)

    return {
        "exam_id": exam_id,
        "status": exam.status,
        "tasks_count": len(tasks),
        "variants_count": exam.variants_count,
        "group_id": exam.group_id,
        "group_name": exam.group_name,
        "class_level": exam.class_level,
        "delivery_mode": exam.delivery_mode,
        "skills_covered": list(skills_set),
    }


def _to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _grade_and_submit_session(
    db: Session,
    session: ExamSession,
    exam: GeneratedExam,
    answers: dict,
    time_spent_seconds: int = 0,
    *,
    commit: bool = True,
) -> dict:
    """Helper to grade answers, compute distractor analytics, and record submission."""
    tasks_raw = session_tasks(exam, session)
    mc_mode = (exam.settings or {}).get("mc_mode", "auto")
    # One verdict per task, stored with the session: results, analytics and the report show these, not a regrade.
    verdicts = grade_session_tasks(tasks_raw, answers, mc_mode)
    correct_count = sum(verdicts.values())
    distractor_analytics: dict = {}
    for t in tasks_raw:
        task_id = t.get("task_id", "")
        if verdicts.get(task_id):
            continue
        try:
            hit = _match_distractor(answers.get(task_id, ""), t.get("distractor_meta") or [])
        except Exception as exc:  # noqa: BLE001 - a hit is analytics, it must never stop a submit
            log.warning("distractor match failed for task %s: %s", task_id, type(exc).__name__)
            hit = None
        if hit:
            distractor_analytics[task_id] = build_analytics_hit(hit)

    total_tasks = len(tasks_raw)
    score = correct_count * 10
    max_score = total_tasks * 10

    SessionRepository(db).submit(
        session,
        answers,
        score,
        max_score,
        correct_count,
        total_tasks,
        time_spent_seconds,
        distractor_analytics=with_grading(distractor_analytics, verdicts),
        commit=commit,
    )
    return {
        "score": score,
        "max_score": max_score,
        "correct_count": correct_count,
        "total_tasks": total_tasks,
        "percentage": round(score / max_score * 100, 1) if max_score else 0.0,
    }


def _exam_started_at(db: Session, exam: GeneratedExam) -> Optional[datetime]:
    """Return the authoritative common start timestamp for one live exam."""
    raw_started_at = (exam.settings or {}).get("started_at")
    if raw_started_at:
        try:
            return _to_naive_utc(datetime.fromisoformat(raw_started_at))
        except (TypeError, ValueError):
            log.warning("Invalid started_at for exam %s", exam.id)

    first_session = (
        db.query(ExamSession.started_at)
        .filter(ExamSession.exam_id == exam.id, ExamSession.started_at.is_not(None))
        .order_by(ExamSession.started_at.asc())
        .first()
    )
    return _to_naive_utc(first_session[0]) if first_session else None


def _live_exam_dashboard_payload(
    db: Session,
    exam: GeneratedExam,
) -> LiveExamDashboardResponse:
    """Build one compact live-exam snapshot from authoritative DB state."""
    started_at = _exam_started_at(db, exam)
    total_time_seconds = (exam.total_time_minutes or 45) * 60

    if exam.status == "active" and started_at:
        elapsed_seconds = max(0, int((datetime.utcnow() - started_at).total_seconds()))
        time_remaining_seconds = max(0, total_time_seconds - elapsed_seconds)
    elif exam.status == "closed":
        time_remaining_seconds = 0
    else:
        # Lobby has not started. The duration shown to the teacher is the
        # configured duration, not a browser-side countdown.
        time_remaining_seconds = total_time_seconds

    statuses = [
        status
        for (status,) in (
            db.query(ExamSession.status)
            .filter(ExamSession.exam_id == exam.id)
            .all()
        )
    ]
    connected_statuses = {"waiting", "in_progress", "submitted"}

    return LiveExamDashboardResponse(
        exam_id=exam.id,
        title=exam.title,
        status=exam.status,
        delivery_mode=exam.delivery_mode or "online",
        group_id=exam.group_id,
        group_name=exam.group_name,
        scheduled_lesson_id=exam.scheduled_lesson_id,
        scheduled_lesson_date=exam.scheduled_lesson_date,
        total_time_seconds=total_time_seconds,
        time_remaining_seconds=time_remaining_seconds,
        connected_count=sum(status in connected_statuses for status in statuses),
        solving_count=sum(status == "in_progress" for status in statuses),
        submitted_count=sum(status == "submitted" for status in statuses),
        started_at=started_at,
    )


def _finalize_exam(db: Session, exam: GeneratedExam) -> int:
    """Atomically close an exam and preserve every started student's work."""
    if exam.status in {"closed", "archived"}:
        return 0
    allowed_statuses = {"active", "lobby"}
    if exam.delivery_mode == "print":
        allowed_statuses = {"active"}
    if exam.status not in allowed_statuses:
        raise HTTPException(409, "Завершить можно только запущенный экзамен")

    total_seconds = (exam.total_time_minutes or 45) * 60
    finalized_count = 0
    if exam.delivery_mode != "print" and exam.status == "active":
        sessions = (
            db.query(ExamSession)
            .filter(ExamSession.exam_id == exam.id)
            .with_for_update()
            .populate_existing()
            .all()
        )
        for session in sessions:
            if session.status in {"in_progress", "waiting"}:
                _grade_and_submit_session(
                    db,
                    session,
                    exam,
                    session.answers or {},
                    min(max(session.time_spent_seconds or 0, 0), total_seconds),
                    commit=False,
                )
                finalized_count += 1

    previous_status = exam.status
    exam.status = "closed"
    package = None
    if exam.delivery_mode == "print":
        package = _get_print_package_or_404(db, exam.id)
        package.status = "completed"
    _record_exam_lifecycle_event(
        db,
        exam,
        {
            "type": "exam_closed",
            "group_id": exam.group_id,
            "group_name": exam.group_name,
            "lesson_id": exam.scheduled_lesson_id,
            "lesson_date": exam.scheduled_lesson_date,
            "details": {
                "previous_status": previous_status,
                "finalized_sessions": finalized_count,
                "delivery_mode": exam.delivery_mode or "online",
                "print_package_version": package.version if package else None,
            },
        },
    )
    # An early interim report is no longer authoritative after finalization.
    db.query(ExamReport).filter(ExamReport.exam_id == exam.id).delete()
    db.commit()
    db.refresh(exam)
    return finalized_count


def _exam_deadline_passed(db: Session, exam: GeneratedExam) -> bool:
    """Has the common timer of an ACTIVE exam run out?  Read-only: takes no lock, writes nothing."""
    if exam.status != "active":
        return False
    exam_started_at = _exam_started_at(db, exam)
    if not exam_started_at:
        return False
    total_seconds = (exam.total_time_minutes or 45) * 60
    return int((datetime.utcnow() - exam_started_at).total_seconds()) >= total_seconds


def _check_and_auto_close_exam(db: Session, exam: Optional[GeneratedExam]) -> bool:
    """
    Checks if an active exam's total duration has expired.
    If expired, marks exam as closed, grades/auto-submits all in_progress/waiting sessions,
    and commits to DB.
    Returns True if exam was closed.

    The exclusive row lock is taken ONLY when there is something to close.  Every student request calls this, so
    locking the shared exam row on each call serialised a whole class on one row (ROADMAP #95); a timer that has
    not run out needs no lock - the next call after the deadline finalizes, and the lock-protected re-check below
    keeps the transition single and deterministic.  Lock order of every writer: exam -> sessions.
    """
    if not exam or exam.status != "active":
        return exam.status == "closed" if exam else False
    if not _exam_deadline_passed(db, exam):
        return False
    # Serialize timeout finalization against a manual close or a concurrent
    # submit.  PostgreSQL row locks make the state transition deterministic.
    locked_exam = (
        db.query(GeneratedExam)
        .filter(GeneratedExam.id == exam.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if not locked_exam or locked_exam.status != "active":
        closed = bool(locked_exam and locked_exam.status == "closed")
        db.commit()  # nothing to do after all: do not keep the exclusive lock until the end of the request
        return closed

    exam_started_at = _exam_started_at(db, locked_exam)
    if not exam_started_at:
        db.commit()
        return False

    now_utc = datetime.utcnow()
    total_seconds = (locked_exam.total_time_minutes or 45) * 60
    elapsed_seconds = int((now_utc - exam_started_at).total_seconds())

    if elapsed_seconds >= total_seconds:
        _finalize_exam(db, locked_exam)
        return True

    db.commit()
    return False


@router.get("/exams",
            summary="Список экзаменов (Библиотека учителя)")
def list_exams(
    group_id: Optional[str] = None,
    unassigned: Optional[bool] = None,   # True = only library templates (group_id IS NULL)
    status: Optional[str] = None,        # draft | active | closed | archived
    include_archived: bool = True,       # return archived along with other statuses for tab counts
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
    include_meta: bool = Query(False),
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """List exams with optional filters. Includes session counts."""
    query = db.query(GeneratedExam).filter(GeneratedExam.org_id == actor.org_id)
    if group_id:
        query = query.filter(GeneratedExam.group_id == group_id)
    if unassigned is True:
        query = query.filter(GeneratedExam.group_id.is_(None))
    if status:
        if status == "draft":
            query = query.filter(GeneratedExam.status.in_(["draft", "variants_ready"]))
        elif status == "active":
            query = query.filter(GeneratedExam.status.in_(["active", "lobby"]))
        else:
            query = query.filter(GeneratedExam.status == status)
    elif not include_archived:
        query = query.filter(GeneratedExam.status != "archived")

    if include_meta and not actor.is_org_admin:
        raise HTTPException(403, "Постраничный каталог доступен только администратору")

    total = query.count() if include_meta else None
    exams = (
        query
        .order_by(
            GeneratedExam.is_pinned.desc(),   # pinned first
            GeneratedExam.created_at.desc()
        )
        .offset(offset if include_meta else 0)
        .limit(limit if include_meta else 100)
        .all()
    )
    # An owner/admin can see the whole organisation.  A teacher can see their
    # own templates and the runs assigned to a group they teach.
    if not actor.is_org_admin:
        # A deleted group may still be referenced by an old archived exam. It is not visible to this teacher, but
        # must not make the complete library unavailable (a 403/404 for a group hides its exams, nothing more).
        decisions = _exam_access_decisions(db, exams, actor)
        exams = [exam for exam in exams if decisions[exam.id]]

    # An open exam is valid only when it has a concrete schedule occurrence.
    # Legacy rows with attempts are intentionally never rewritten here.
    for e in exams:
        if e.status in ("active", "lobby") and (
            not e.group_id or not e.scheduled_lesson_id or not e.scheduled_lesson_date
        ):
            _invalidate_unbound_open_exam(db, e)
        elif e.status == "active":
            _check_and_auto_close_exam(db, e)

    # Count submitted sessions per exam in one query
    from sqlalchemy import func as sqlfunc
    exam_ids = [e.id for e in exams]
    session_counts: dict[str, int] = {}
    if exam_ids:
        rows = (
            db.query(ExamSession.exam_id, sqlfunc.count(ExamSession.id))
            .filter(ExamSession.exam_id.in_(exam_ids), ExamSession.status == "submitted")
            .group_by(ExamSession.exam_id)
            .all()
        )
        session_counts = {r[0]: r[1] for r in rows}

    summaries = [
        ExamSummaryResponse(
            exam_id=e.id,
            title=e.title,
            status=e.status,
            delivery_mode=e.delivery_mode or "online",
            # Access code is a student capability.  Organisation-wide
            # monitoring deliberately does not grant an administrator the
            # ability to distribute or reopen a classroom run.
            access_code=e.access_code if actor.role == "teacher" else None,
            tasks_per_variant=e.tasks_per_variant,
            variants_count=e.variants_count,
            total_time_minutes=e.total_time_minutes or 45,
            group_id=e.group_id,
            group_name=e.group_name,
            class_level=e.class_level,
            teacher_name=e.teacher_name,
            scheduled_lesson_id=e.scheduled_lesson_id,
            scheduled_lesson_date=e.scheduled_lesson_date,
            is_pinned=bool(e.is_pinned),
            source_exam_id=e.source_exam_id,
            total_sessions=session_counts.get(e.id, 0),
            skills_covered=list(e.skills_covered or []),
            recent_events=_recent_exam_lifecycle_events(db, e),
            created_at=e.created_at,
        )
        for e in exams
    ]
    if include_meta:
        return {
            "items": summaries,
            "total": total or 0,
            "limit": limit,
            "offset": offset,
        }
    return summaries


def _monitor_event_ids(
    *,
    org_id: str,
    date_from: Optional[date],
    date_to: Optional[date],
    event_types: Optional[list[str]] = None,
):
    """Build a scalar subquery of exams with matching immutable events."""
    query = select(ExamLifecycleEvent.exam_id).where(ExamLifecycleEvent.org_id == org_id)
    if event_types:
        query = query.where(ExamLifecycleEvent.event_type.in_(event_types))
    if date_from:
        local_start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=_BUSINESS_TIMEZONE)
        query = query.where(ExamLifecycleEvent.occurred_at >= local_start.astimezone(timezone.utc).replace(tzinfo=None))
    if date_to:
        next_day = date_to + timedelta(days=1)
        local_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=_BUSINESS_TIMEZONE)
        query = query.where(ExamLifecycleEvent.occurred_at < local_end.astimezone(timezone.utc).replace(tzinfo=None))
    return query


def _monitor_period_clause(
    *,
    org_id: str,
    date_from: Optional[date],
    date_to: Optional[date],
):
    """Select a run if any verifiable lifecycle fact falls in the period."""
    if not date_from and not date_to:
        return None
    conditions = []
    if date_from:
        local_start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=_BUSINESS_TIMEZONE)
        conditions.append(GeneratedExam.created_at >= local_start.astimezone(timezone.utc).replace(tzinfo=None))
        conditions.append(GeneratedExam.scheduled_lesson_date >= date_from.isoformat())
    if date_to:
        next_day = date_to + timedelta(days=1)
        local_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=_BUSINESS_TIMEZONE)
        conditions.append(GeneratedExam.created_at < local_end.astimezone(timezone.utc).replace(tzinfo=None))
        conditions.append(GeneratedExam.scheduled_lesson_date <= date_to.isoformat())
    # When both endpoints are present, dates must be inside the interval, not
    # merely above one boundary or below the other.
    date_conditions = []
    if date_from and date_to:
        local_start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=_BUSINESS_TIMEZONE)
        next_day = date_to + timedelta(days=1)
        local_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=_BUSINESS_TIMEZONE)
        date_conditions.extend([
            and_(
                GeneratedExam.created_at >= local_start.astimezone(timezone.utc).replace(tzinfo=None),
                GeneratedExam.created_at < local_end.astimezone(timezone.utc).replace(tzinfo=None),
            ),
            and_(
                GeneratedExam.scheduled_lesson_date >= date_from.isoformat(),
                GeneratedExam.scheduled_lesson_date <= date_to.isoformat(),
            ),
        ])
    else:
        date_conditions = conditions
    date_conditions.append(GeneratedExam.id.in_(_monitor_event_ids(org_id=org_id, date_from=date_from, date_to=date_to)))
    return or_(*date_conditions)


def _monitor_summary(db: Session, exam: GeneratedExam, submitted_count: int) -> ExamSummaryResponse:
    return ExamSummaryResponse(
        exam_id=exam.id,
        title=exam.title,
        status=exam.status,
        delivery_mode=exam.delivery_mode or "online",
        access_code=None,
        tasks_per_variant=exam.tasks_per_variant,
        variants_count=exam.variants_count,
        total_time_minutes=exam.total_time_minutes or 45,
        group_id=exam.group_id,
        group_name=exam.group_name,
        class_level=exam.class_level,
        is_pinned=bool(exam.is_pinned),
        teacher_name=exam.teacher_name,
        scheduled_lesson_id=exam.scheduled_lesson_id,
        scheduled_lesson_date=exam.scheduled_lesson_date,
        source_exam_id=exam.source_exam_id,
        total_sessions=submitted_count,
        skills_covered=list(exam.skills_covered or []),
        recent_events=_recent_exam_lifecycle_events(db, exam),
        created_at=exam.created_at,
    )


@router.get(
    "/admin/exams/monitor",
    response_model=AdminExamMonitorResponse,
    summary="Постраничный исторический мониторинг экзаменов организации",
)
def get_admin_exam_monitor(
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    state: str = Query("all", pattern="^(all|active|lobby|closed|changes)$"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Read-only monitor with one status dimension and one time dimension.

    ``active`` and ``lobby`` deliberately mean the current operational state;
    the web app resets the visible period to today before requesting either.
    ``closed`` and ``changes`` are historical filters backed by immutable
    events, with a scheduled-date fallback for legacy closed runs.
    """
    _require_exam_admin_observer(actor)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "Начало периода не может быть позже его окончания")

    period_clause = _monitor_period_clause(org_id=actor.org_id, date_from=date_from, date_to=date_to)
    base = db.query(GeneratedExam).filter(GeneratedExam.org_id == actor.org_id)
    scoped = base.filter(period_clause) if period_clause is not None else base

    changes_ids = _monitor_event_ids(
        org_id=actor.org_id,
        date_from=date_from,
        date_to=date_to,
        event_types=["lesson_rescheduled", "lesson_cancelled", "exam_rescheduled"],
    )
    closed_ids = _monitor_event_ids(
        org_id=actor.org_id,
        date_from=date_from,
        date_to=date_to,
        event_types=["exam_closed"],
    )

    if state == "active":
        scoped = scoped.filter(GeneratedExam.status == "active")
    elif state == "lobby":
        scoped = scoped.filter(GeneratedExam.status == "lobby")
    elif state == "closed":
        legacy_closed = GeneratedExam.status == "closed"
        if date_from or date_to:
            scoped = base.filter(
                or_(
                    GeneratedExam.id.in_(closed_ids),
                    and_(legacy_closed, period_clause),
                )
            )
        else:
            scoped = scoped.filter(legacy_closed)
    elif state == "changes":
        scoped = base.filter(GeneratedExam.id.in_(changes_ids))

    # Auto-finalisation remains server authoritative even if no teacher page
    # is open. Re-evaluate the matching page after these transitions.
    candidate_rows = scoped.order_by(GeneratedExam.updated_at.desc()).all()
    for exam in candidate_rows:
        if exam.status == "active":
            _check_and_auto_close_exam(db, exam)

    # The query may have changed due to an auto-close, so rebuild it exactly
    # once for consistent list rows and KPI counts.
    if state == "active":
        scoped = (base.filter(period_clause) if period_clause is not None else base).filter(GeneratedExam.status == "active")
    elif state == "closed" and (date_from or date_to):
        scoped = base.filter(or_(GeneratedExam.id.in_(closed_ids), and_(GeneratedExam.status == "closed", period_clause)))
    elif state == "closed":
        scoped = (base.filter(period_clause) if period_clause is not None else base).filter(GeneratedExam.status == "closed")

    total = scoped.count()
    rows = (
        scoped.order_by(GeneratedExam.updated_at.desc(), GeneratedExam.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    exam_ids = [exam.id for exam in rows]
    submitted_counts: dict[str, int] = {}
    if exam_ids:
        from sqlalchemy import func as sqlfunc
        submitted_counts = {
            row[0]: row[1]
            for row in (
                db.query(ExamSession.exam_id, sqlfunc.count(ExamSession.id))
                .filter(ExamSession.exam_id.in_(exam_ids), ExamSession.status == "submitted")
                .group_by(ExamSession.exam_id)
                .all()
            )
        }

    metric_base = base.filter(period_clause) if period_clause is not None else base
    counts = {
        "all": metric_base.count(),
        "active": metric_base.filter(GeneratedExam.status == "active").count(),
        "lobby": metric_base.filter(GeneratedExam.status == "lobby").count(),
        "closed": metric_base.filter(GeneratedExam.status == "closed").count(),
        "changes": base.filter(GeneratedExam.id.in_(changes_ids)).count(),
    }
    return AdminExamMonitorResponse(
        items=[_monitor_summary(db, exam, submitted_counts.get(exam.id, 0)) for exam in rows],
        total=total,
        limit=limit,
        offset=offset,
        period_from=date_from.isoformat() if date_from else None,
        period_to=date_to.isoformat() if date_to else None,
        counts=counts,
    )


@router.get(
    "/exams/live-summary",
    response_model=List[LiveExamDashboardResponse],
    summary="Оперативная сводка экзаменов для панели преподавателя",
)
def get_live_exam_dashboard_summary(
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Return live exams, prepared paper deliveries and recently closed runs.

    The endpoint is intentionally separate from the library list: the
    dashboard needs a short operational snapshot, while the library may hold
    an arbitrary number of drafts and archived templates.  A prepared paper
    package is operational too: the teacher must either issue it during its
    lesson or resolve an overdue delivery.  A recently closed run remains
    visible for six hours so the teacher sees the definitive ``exam finished``
    state instead of a card abruptly disappearing at zero.
    """
    recent_cutoff = datetime.utcnow() - timedelta(hours=6)
    candidates = (
        db.query(GeneratedExam)
        .filter(
            GeneratedExam.org_id == actor.org_id,
            or_(
                GeneratedExam.status.in_(["lobby", "print_ready", "active"]),
                and_(
                    GeneratedExam.status == "closed",
                    GeneratedExam.updated_at >= recent_cutoff,
                ),
            ),
        )
        .order_by(GeneratedExam.updated_at.desc())
        .limit(100)
        .all()
    )

    result: list[LiveExamDashboardResponse] = []
    eligible: list[GeneratedExam] = []

    for exam in candidates:
        if exam.status in {"lobby", "active"} and (
            not exam.group_id or not exam.scheduled_lesson_id or not exam.scheduled_lesson_date
        ):
            invalidated = _invalidate_unbound_open_exam(db, exam)
            # A historic active run with real attempts is never rewritten or
            # hidden from its teacher.  It cannot accept new public entries
            # without a lesson binding, but it stays observable until closed.
            if invalidated or exam.status != "active" or not _has_any_sessions(db, exam.id):
                continue
        # A paper package is always issued for one concrete lesson.  Do not
        # expose malformed legacy rows as actionable dashboard cards; unlike
        # a public lobby, the package must not be reset implicitly here.
        if exam.status == "print_ready" and (
            not exam.group_id or not exam.scheduled_lesson_id or not exam.scheduled_lesson_date
        ):
            continue
        eligible.append(exam)

    # One concurrent round of group lookups for the whole snapshot, after the read transaction was ended.  A deleted
    # group on a historical exam must never make the complete operational dashboard unavailable.
    decisions = _exam_access_decisions(db, eligible, actor)

    for exam in eligible:
        if not decisions[exam.id]:
            continue

        _check_and_auto_close_exam(db, exam)
        if exam.status not in {"lobby", "print_ready", "active", "closed"}:
            continue

        started_at = _exam_started_at(db, exam)
        if exam.status == "closed" and (not started_at or started_at < recent_cutoff):
            continue

        result.append(_live_exam_dashboard_payload(db, exam))

    # Active work is always the highest-priority item.  Recently completed
    # cards follow it and are read-only.
    order = {"active": 0, "print_ready": 1, "lobby": 1, "closed": 2}
    return sorted(result, key=lambda item: (order.get(item.status, 9), item.title.lower()))


@router.post("/exams/{exam_id}/pin",
             summary="Переключить закрепление (избранное)")
def toggle_pin_exam(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Toggle is_pinned on a teacher's exam."""
    exam_repo = ExamRepository(db)
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    exam.is_pinned = not bool(exam.is_pinned)
    db.commit()
    return {"exam_id": exam_id, "is_pinned": exam.is_pinned}


class CloneExamRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)
    group_id: Optional[str] = None      # optionally assign to a group immediately
    group_name: Optional[str] = None
    class_level: Optional[int] = None


class RenameExamRequest(BaseModel):
    """A title can change only while the exam is still an editable draft."""

    title: str = Field(..., min_length=1, max_length=300)


def _next_clone_title(db: Session, source: GeneratedExam, org_id: str) -> str:
    """Create a clear, collision-free default title for a cloned exam.

    A clone is a new editable teaching artefact.  Keeping the source title in
    its name makes the relationship obvious in the library, while the numeric
    suffix prevents a teacher who makes several copies from getting a stack of
    indistinguishable ``(копия)`` cards.
    """
    source_title = str(source.title or "Экзамен").strip() or "Экзамен"

    def with_suffix(suffix: str) -> str:
        # The source title may already use all 300 allowed characters.  Keep
        # room for the suffix instead of turning a valid source into an
        # invalid clone at database-commit time.
        available = max(1, 300 - len(suffix))
        return f"{source_title[:available].rstrip()}{suffix}"

    base = with_suffix(" — копия")
    existing_titles = {
        str(row[0])
        for row in db.query(GeneratedExam.title)
        .filter(GeneratedExam.org_id == org_id)
        .all()
        if row[0]
    }
    if base not in existing_titles:
        return base

    number = 2
    candidate = with_suffix(f" — копия {number}")
    while candidate in existing_titles:
        number += 1
        candidate = with_suffix(f" — копия {number}")
    return candidate


@router.patch("/exams/{exam_id}/title", summary="Переименовать черновик экзамена")
def rename_exam(
    exam_id: str,
    req: RenameExamRequest,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Rename an editable draft without changing its tasks or variants.

    Once a lobby is opened, students may already see the title.  Once a paper
    package is prepared, its title is part of the signed PDF snapshot.  Those
    historical runs are intentionally immutable: create a clone to make a
    revised version instead.
    """
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    if exam.status not in {"draft", "variants_ready"}:
        raise HTTPException(
            409,
            "Переименование доступно только для черновика. Для проведённого, подготовленного или архивного экзамена создайте копию.",
        )

    title = req.title.strip()
    if not title:
        raise HTTPException(422, "Название экзамена не может состоять только из пробелов")

    previous_title = exam.title
    if title == previous_title:
        return {"exam_id": exam.id, "title": exam.title, "status": exam.status}

    exam.title = title
    _record_exam_lifecycle_event(
        db,
        exam,
        {
            "type": "exam_renamed",
            "group_id": exam.group_id,
            "group_name": exam.group_name,
            "details": {"previous_title": previous_title, "title": title},
        },
        actor_id=actor.user_id,
    )
    db.commit()
    db.refresh(exam)
    return {"exam_id": exam.id, "title": exam.title, "status": exam.status}


@router.post("/exams/{exam_id}/clone",
             summary="Клонировать экзамен (шаблон)")
def clone_exam(
    exam_id: str,
    req: CloneExamRequest,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """
    Duplicate an exam as a new editable draft:
    - copies variants + settings
    - clears group, lesson, access code, sessions and print package
    - keeps a lineage link to the source, but never changes the source
    - optionally binds to a new group
    """
    src = _get_exam_for_teacher_operator(db, exam_id, actor)
    if src.status == "archived":
        raise HTTPException(409, "Экзамен находится в архиве. Сначала восстановите его, затем создайте копию.")
    group = _await(require_group_access(actor, req.group_id), db) if req.group_id else None
    requested_title = req.title.strip() if req.title is not None else None
    if requested_title is not None and not requested_title:
        raise HTTPException(422, "Название экзамена не может состоять только из пробелов")

    new_id = _new_exam_id()
    clone = GeneratedExam(
        id=new_id,
        title=requested_title or _next_clone_title(db, src, actor.org_id),
        teacher_name=src.teacher_name,
        org_id=actor.org_id,
        created_by=actor.user_id,
        group_id=str(group["id"]) if group else None,
        group_name=group.get("name") if group else None,
        class_level=group.get("class_level") if group else (req.class_level or src.class_level),
        source_mode=src.source_mode,
        delivery_mode=src.delivery_mode or "online",
        variants=deepcopy(src.variants or []),
        # A print package itself is an immutable run artefact and is never
        # copied.  Its editable layout is safe to carry into a fresh draft.
        settings={
            key: value
            for key, value in deepcopy(src.settings or {}).items()
            # Lifecycle data belongs to the source run, not to the reusable
            # draft made from it. The print layout remains intentionally.
            if key not in {"lifecycle_events", "started_at"}
        },
        skills_covered=deepcopy(src.skills_covered or []),
        toc_ids_used=deepcopy(src.toc_ids_used or []),
        status="variants_ready" if src.variants and len(src.variants) > 0 else "draft",
        total_time_minutes=src.total_time_minutes,
        variants_count=src.variants_count,
        tasks_per_variant=src.tasks_per_variant,
        subject=src.subject,
        source_exam_id=exam_id,   # track lineage
        is_pinned=False,
    )
    # ``exam_lifecycle_events.exam_id`` has a real foreign key in PostgreSQL.
    # Flush the new row before appending its first audit event; committing both
    # afterwards still keeps clone and history atomic as one transaction.
    _add_new_exam(db, clone)
    new_id = clone.id  # an id collision may have drawn a new one
    _record_exam_lifecycle_event(
        db,
        clone,
        {
            "type": "exam_created",
            "group_id": clone.group_id,
            "group_name": clone.group_name,
            "details": {"initial_status": clone.status, "source_exam_id": exam_id, "operation": "clone"},
        },
        actor_id=actor.user_id,
    )
    db.commit()
    db.refresh(clone)
    log.info("Exam cloned: %s → %s, group=%s", exam_id, new_id, req.group_id)
    return {
        "exam_id": clone.id,
        "title": clone.title,
        "status": clone.status,
        "source_exam_id": exam_id,
        "group_id": clone.group_id,
        "group_name": clone.group_name,
    }


class AssignExamRequest(BaseModel):
    group_id: str
    group_name: Optional[str] = None
    class_level: Optional[int] = None
    teacher_name: Optional[str] = None
    subject: Optional[str] = None
    # ``publish`` opens the student lobby.  It is valid only together with a
    # real resolved lesson occurrence; a group by itself is never enough.
    publish: bool = False
    lesson_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    lesson_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


@router.post("/exams/{exam_id}/assign",
             summary="Назначить экзамен группе")
def assign_exam(
    exam_id: str,
    req: AssignExamRequest,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """
    Bind a draft to a group and, when requested, to a concrete lesson.

    Rebinding a lobby is permitted only before any student has entered it.
    Once a session exists, a copy is required so an earlier run is never
    silently repurposed for another lesson or group.
    """
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    # Print delivery has a different lifecycle from an online exam: it must
    # never receive an access code or enter an online lobby, including through
    # the legacy non-publishing assignment path.  A paper package is bound to
    # one concrete lesson exclusively by ``/print-package/prepare``.
    if exam.delivery_mode == "print":
        raise HTTPException(
            422,
            "Для формата «Печать» выберите занятие через подготовку печатного комплекта",
        )
    group = _await(require_group_access(actor, req.group_id), db)
    if _has_any_sessions(db, exam_id):
        raise HTTPException(
            409,
            f"Экзамен уже имеет попытки группы '{exam.group_name}'. "
            "Создайте копию (клон), чтобы провести для другой группы."
        )
    if exam.status not in ("draft", "variants_ready", "lobby"):
        raise HTTPException(409, "Назначить или перенести можно только экзамен до его запуска")
    if exam.status == "lobby" and not req.publish:
        raise HTTPException(409, "Для экзамена в лобби выберите новое занятие и подтвердите перенос")
    if req.publish and (not req.lesson_id or not req.lesson_date):
        raise HTTPException(422, "Для лобби выберите конкретное занятие в расписании")

    previous_status = exam.status
    previous_group_id = exam.group_id
    previous_lesson_id = exam.scheduled_lesson_id
    previous_lesson_date = exam.scheduled_lesson_date
    canonical_group = None
    if req.publish:
        # BEFORE any change to the exam: nothing may be pending in the session over a network call
        canonical_group, _ = _await(_resolve_lesson_occurrence(
            actor,
            group_id=str(group["id"]),
            lesson_id=req.lesson_id,
            lesson_date=req.lesson_date,
        ), db)

    exam = _lock_exam_for_transition(
        db,
        exam,
        {"draft", "variants_ready", "lobby"},
        "Назначить или перенести можно только экзамен до его запуска",
        no_sessions_refusal="Экзамен уже имеет попытки. Создайте копию (клон), чтобы провести для другой группы.",
    )
    previous_status = exam.status
    previous_group_id = exam.group_id
    previous_lesson_id = exam.scheduled_lesson_id
    previous_lesson_date = exam.scheduled_lesson_date

    exam.group_id = str(group["id"])
    exam.group_name = group.get("name")
    exam.class_level = group.get("class_level")
    # The group service is authoritative for the responsible teacher. Do not
    # trust a name supplied by the browser for an administrative audit trail.
    exam.teacher_name = group.get("teacher_name") or req.teacher_name or exam.teacher_name
    if req.subject:
        exam.subject = req.subject

    access_code = None
    if req.publish:
        _ensure_lesson_slot_is_available(
            db,
            exam,
            org_id=actor.org_id,
            lesson_id=req.lesson_id,
            lesson_date=req.lesson_date,
        )
        exam.group_id = str(canonical_group["id"])
        exam.group_name = canonical_group.get("name")
        exam.class_level = canonical_group.get("class_level")
        exam.teacher_name = canonical_group.get("teacher_name") or exam.teacher_name
        exam.scheduled_lesson_id = req.lesson_id
        exam.scheduled_lesson_date = req.lesson_date
        is_rebinding = bool(
            previous_lesson_id
            and (
                previous_group_id != str(canonical_group["id"])
                or previous_lesson_id != req.lesson_id
                or previous_lesson_date != req.lesson_date
            )
        )
        # A code is a public capability, not a permanent exam identifier.
        # Rotating it on a move prevents an old QR/link from opening the new
        # lesson's lobby by accident.
        access_code = _generate_access_code(db) if is_rebinding else (exam.access_code or _generate_access_code(db))
        exam.access_code = access_code
        exam.status = "lobby"
        _record_exam_lifecycle_event(
            db,
            exam,
            {
                "type": "exam_rescheduled" if previous_lesson_id else "exam_lobby_opened",
                "group_id": exam.group_id,
                "group_name": exam.group_name,
                "lesson_id": exam.scheduled_lesson_id,
                "lesson_date": exam.scheduled_lesson_date,
                "previous_lesson_id": previous_lesson_id,
                "previous_lesson_date": previous_lesson_date,
                "details": {
                    "previous_status": previous_status,
                    "operation": "assign",
                    "access_code_rotated": is_rebinding,
                },
            },
            actor_id=actor.user_id,
        )
    else:
        # A draft may be prepared for a group, but it is not a scheduled
        # public run until the teacher explicitly selects a lesson.
        _reset_lobby_to_draft(
            db,
            exam,
            actor_id=actor.user_id,
            lifecycle_event={
                "type": "exam_unbound",
                "group_id": exam.group_id,
                "group_name": exam.group_name,
                "lesson_id": previous_lesson_id,
                "lesson_date": previous_lesson_date,
                "details": {"previous_status": previous_status, "operation": "assign"},
            } if previous_lesson_id else None,
        )

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _binding_conflict(exc) from exc
    db.refresh(exam)

    log.info("Exam %s assigned to group %s, publish=%s", exam_id, req.group_id, req.publish)
    return {
        "exam_id": exam_id,
        "status": exam.status,
        "group_id": exam.group_id,
        "group_name": exam.group_name,
        "access_code": access_code,
        "scheduled_lesson_id": exam.scheduled_lesson_id,
        "scheduled_lesson_date": exam.scheduled_lesson_date,
        "url": f"/e/{access_code}" if access_code else None,
    }


@router.post("/exams/{exam_id}/publish", response_model=PublishExamResponse,
             summary="Опубликовать экзамен → получить access_code")
def publish_exam(
    exam_id: str,
    req: PublishExamRequest,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    exam_repo = ExamRepository(db)
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    if exam.delivery_mode == "print":
        raise HTTPException(409, "Печатный экзамен не публикуется в лобби. Подготовьте комплект для занятия.")
    if exam.status not in ("draft", "variants_ready", "lobby"):
        raise HTTPException(409, "В лобби можно вывести только экзамен до его запуска")

    target_group_id = req.group_id or exam.group_id
    if not target_group_id:
        raise HTTPException(
            400,
            "Нельзя активировать экзамен без привязки к группе. Сначала привяжите экзамен к классу."
        )

    if not req.lesson_id or not req.lesson_date:
        raise HTTPException(422, "Для лобби выберите конкретное занятие в расписании")
    if _has_any_sessions(db, exam_id):
        raise HTTPException(409, "Экзамен уже имеет попытки. Для нового урока создайте его копию")
    group, _ = _await(_resolve_lesson_occurrence(
        actor,
        group_id=target_group_id,
        lesson_id=req.lesson_id,
        lesson_date=req.lesson_date,
    ), db)
    _ensure_lesson_slot_is_available(
        db,
        exam,
        org_id=actor.org_id,
        lesson_id=req.lesson_id,
        lesson_date=req.lesson_date,
    )
    exam = _lock_exam_for_transition(
        db,
        exam,
        {"draft", "variants_ready", "lobby"},
        "В лобби можно вывести только экзамен до его запуска",
        no_sessions_refusal="Экзамен уже имеет попытки. Для нового урока создайте его копию",
    )
    previous_status = exam.status
    previous_group_id = exam.group_id
    previous_lesson_id = exam.scheduled_lesson_id
    previous_lesson_date = exam.scheduled_lesson_date
    is_rebinding = bool(
        previous_lesson_id
        and (
            previous_group_id != str(group["id"])
            or previous_lesson_id != req.lesson_id
            or previous_lesson_date != req.lesson_date
        )
    )
    code = _generate_access_code(db) if is_rebinding else (exam.access_code or _generate_access_code(db))
    if req.subject:
        exam.subject = req.subject
    exam.group_id = str(group["id"])
    exam.group_name = group.get("name")
    exam.class_level = group.get("class_level")
    # The responsible teacher comes from the canonical group, never from a
    # potentially stale client-side label.
    exam.teacher_name = group.get("teacher_name") or req.teacher_name or exam.teacher_name
    exam.scheduled_lesson_id = req.lesson_id
    exam.scheduled_lesson_date = req.lesson_date
    exam.access_code = code
    exam.status = "lobby"
    _record_exam_lifecycle_event(
        db,
        exam,
        {
            "type": "exam_rescheduled" if previous_lesson_id else "exam_lobby_opened",
            "group_id": exam.group_id,
            "group_name": exam.group_name,
            "lesson_id": exam.scheduled_lesson_id,
            "lesson_date": exam.scheduled_lesson_date,
            "previous_lesson_id": previous_lesson_id,
            "previous_lesson_date": previous_lesson_date,
            "details": {
                "previous_status": previous_status,
                "operation": "publish",
                "access_code_rotated": is_rebinding,
            },
        },
        actor_id=actor.user_id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _binding_conflict(exc) from exc
    db.refresh(exam)

    return PublishExamResponse(
        exam_id=exam.id,
        title=exam.title,
        status=exam.status,
        access_code=exam.access_code,
        tasks_per_variant=exam.tasks_per_variant,
        variants_count=exam.variants_count,
        total_time_minutes=exam.total_time_minutes or 45,
        group_id=exam.group_id,
        group_name=exam.group_name,
        class_level=exam.class_level,
        created_at=exam.created_at,
        url=f"/e/{exam.access_code}",
    )


@router.post("/exams/{exam_id}/open-lobby", summary="Запланировать экзамен и открыть лобби")
def open_exam_lobby(
    exam_id: str,
    req: LessonBindingRequest,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    exam = _get_exam_for_teacher_operator(db, exam_id, actor, "open-lobby")
    if exam.delivery_mode == "print":
        raise HTTPException(409, "Печатный экзамен не использует лобби. Подготовьте печатный комплект.")

    if not exam.group_id:
        raise HTTPException(
            400,
            "Нельзя открыть лобби для экзамена без группы. Сначала привяжите экзамен к группе."
        )

    if exam.status not in ("draft", "variants_ready", "lobby"):
        raise HTTPException(409, "Открыть лобби можно только до начала экзамена")
    if _has_any_sessions(db, exam_id):
        raise HTTPException(409, "В лобби уже есть участники. Для другого занятия создайте копию экзамена")
    previous_status = exam.status
    previous_group_id = exam.group_id
    previous_lesson_id = exam.scheduled_lesson_id
    previous_lesson_date = exam.scheduled_lesson_date
    group, _ = _await(_resolve_lesson_occurrence(
        actor,
        group_id=str(exam.group_id),
        lesson_id=req.lesson_id,
        lesson_date=req.lesson_date,
        substitute_bound_lesson=(previous_lesson_id, previous_lesson_date) if previous_lesson_id and previous_lesson_date else None,
    ), db)
    _ensure_lesson_slot_is_available(
        db,
        exam,
        org_id=actor.org_id,
        lesson_id=req.lesson_id,
        lesson_date=req.lesson_date,
    )
    exam = _lock_exam_for_transition(
        db,
        exam,
        {"draft", "variants_ready", "lobby"},
        "Открыть лобби можно только до начала экзамена",
        no_sessions_refusal="В лобби уже есть участники. Для другого занятия создайте копию экзамена",
    )
    previous_status = exam.status
    previous_group_id = exam.group_id
    previous_lesson_id = exam.scheduled_lesson_id
    previous_lesson_date = exam.scheduled_lesson_date
    exam.group_id = str(group["id"])
    exam.group_name = group.get("name")
    exam.class_level = group.get("class_level")
    exam.teacher_name = group.get("teacher_name") or exam.teacher_name
    exam.scheduled_lesson_id = req.lesson_id
    exam.scheduled_lesson_date = req.lesson_date
    is_rebinding = bool(
        previous_lesson_id
        and (
            previous_group_id != str(group["id"])
            or previous_lesson_id != req.lesson_id
            or previous_lesson_date != req.lesson_date
        )
    )
    if is_rebinding or not exam.access_code:
        exam.access_code = _generate_access_code(db)
    exam.status = "lobby"
    _record_exam_lifecycle_event(
        db,
        exam,
        {
            "type": "exam_rescheduled" if previous_lesson_id else "exam_lobby_opened",
            "group_id": exam.group_id,
            "group_name": exam.group_name,
            "lesson_id": exam.scheduled_lesson_id,
            "lesson_date": exam.scheduled_lesson_date,
            "previous_lesson_id": previous_lesson_id,
            "previous_lesson_date": previous_lesson_date,
            "details": {
                "previous_status": previous_status,
                "operation": "open_lobby",
                "access_code_rotated": is_rebinding,
            },
        },
        actor_id=actor.user_id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _binding_conflict(exc) from exc
    db.refresh(exam)
    return {
        "status": "lobby",
        "access_code": exam.access_code if actor.role == "teacher" else None,
        "url": f"/e/{exam.access_code}",
    }


@router.post(
    "/exams/{exam_id}/entry-limits/reset",
    summary="Сбросить ограничения входа учеников (после ошибочных попыток в классе)",
)
def reset_exam_entry_limits(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Teacher's escape hatch from the entry rate limits (#33).

    A classmate behind the same school address can use up the failure budget of the exam code on purpose.  The
    teacher of the exam clears every counter of its code: the limits start again from zero, nothing else changes.
    Only the group's teacher may do it, and only while the exam is open to students.
    """
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    if not exam.access_code or exam.status not in ("lobby", "active"):
        raise HTTPException(409, "Ограничения входа можно сбросить только для открытого экзамена")
    reset_entry_limits(exam.access_code)
    _record_exam_lifecycle_event(
        db,
        exam,
        {"type": "entry_limits_reset", "details": {"operation": "entry_limits_reset"}},
        actor_id=actor.user_id,
    )
    db.commit()
    return {"exam_id": exam.id, "reset": True}


@router.post(
    "/exams/{exam_id}/delivery/cancel",
    summary="Отменить непройденное проведение экзамена",
)
def cancel_exam_delivery(
    exam_id: str,
    req: CancelExamDeliveryRequest,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Revoke an unstarted lesson delivery without rewriting exam history.

    Online lobbies lose their public code and return to an editable draft.
    A paper package is immutable once generated; it is therefore voided and
    the teacher must clone the exam for a future paper delivery.  Neither path
    can run after a student session/attempt exists.
    """
    _get_exam_for_teacher_operator(db, exam_id, actor, "cancel-delivery")
    exam = (
        db.query(GeneratedExam)
        .filter(GeneratedExam.id == exam_id, GeneratedExam.org_id == actor.org_id)
        .with_for_update()
        .populate_existing()  # re-read under the lock: the state checked below is the committed one
        .first()
    )
    if not exam:
        raise HTTPException(404, "Экзамен не найден")

    if exam.status not in {"lobby", "print_ready"}:
        raise HTTPException(409, "Отменить проведение можно только до запуска экзамена")
    if _has_any_sessions(db, exam.id):
        raise HTTPException(
            409,
            "К экзамену уже присоединились ученики. Для нового занятия создайте копию экзамена.",
        )

    reason = req.reason.strip()
    if len(reason) < 3:
        raise HTTPException(422, "Укажите короткую причину отмены проведения")

    was_print = exam.delivery_mode == "print" or exam.status == "print_ready"
    if was_print:
        _void_print_package_for_cancelled_lesson(
            db,
            exam,
            reason=reason,
            actor_id=actor.user_id,
            disposition="delivery_cancelled",
        )
    else:
        _reset_lobby_to_draft(
            db,
            exam,
            actor_id=actor.user_id,
            lifecycle_event={
                "type": "exam_delivery_cancelled",
                "group_id": exam.group_id,
                "group_name": exam.group_name,
                "lesson_id": exam.scheduled_lesson_id,
                "lesson_date": exam.scheduled_lesson_date,
                "reason": reason,
                "details": {"delivery_mode": "online", "access_code_revoked": bool(exam.access_code)},
            },
        )

    db.commit()
    db.refresh(exam)
    return {
        "exam_id": exam.id,
        "status": exam.status,
        "delivery_mode": exam.delivery_mode or "online",
        "requires_clone_for_print": was_print,
    }


@router.post("/exams/{exam_id}/start-live", summary="Синхронный старт экзамена из лобби")
def start_exam_live(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    exam = _get_exam_for_teacher_operator(db, exam_id, actor, "start-live")
    if exam.delivery_mode == "print":
        raise HTTPException(409, "Печатный экзамен запускается кнопкой «Начать печатный экзамен».")

    if exam.status != "lobby":
        raise HTTPException(409, "Начать можно только экзамен, открытый в лобби")
    _await(_require_lesson_in_progress(_exam_view(exam), actor), db)
    exam = _lock_exam_for_transition(db, exam, {"lobby"}, "Экзамен уже запущен или больше не находится в лобби")
    exam.status = "active"
    sessions = SessionRepository(db).list_by_exam(exam_id)
    started_count = 0
    now = datetime.utcnow()
    st = dict(exam.settings or {})
    st["started_at"] = now.isoformat()
    exam.settings = st
    for s in sessions:
        if s.status == "waiting":
            s.status = "in_progress"
            s.started_at = now
            started_count += 1

    _record_exam_lifecycle_event(
        db,
        exam,
        {
            "type": "exam_started",
            "group_id": exam.group_id,
            "group_name": exam.group_name,
            "lesson_id": exam.scheduled_lesson_id,
            "lesson_date": exam.scheduled_lesson_date,
            "details": {"waiting_students_started": started_count},
        },
        actor_id=actor.user_id,
    )

    db.commit()
    db.refresh(exam)
    return {"status": "active", "started_count": started_count}


@router.get("/exams/{exam_id}/lobby-status", summary="Статус лобби и список подключенных учеников")
def get_exam_lobby_status(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    exam = _get_exam_for_actor(db, exam_id, actor, "lobby-status")
    if exam.delivery_mode == "print":
        raise HTTPException(409, "У печатного экзамена нет лобби и ученических онлайн-сессий.")

    # Check and auto-close if exam duration expired
    _check_and_auto_close_exam(db, exam)

    _invalidate_unbound_open_exam(db, exam)

    sessions = SessionRepository(db).list_by_exam(exam_id)
    session_items = []
    earliest_started_at = None

    for s in sessions:
        disp_id = s.display_id
        if not disp_id and s.student_id and s.student_id.startswith("AL-"):
            disp_id = s.student_id

        if s.started_at:
            s_naive = _to_naive_utc(s.started_at)
            if s_naive and (not earliest_started_at or s_naive < earliest_started_at):
                earliest_started_at = s_naive

        session_items.append({
            "session_id": s.id,
            "student_id": s.student_id,
            "display_id": disp_id,
            "student_name": s.student_name,
            "status": s.status,
            "variant_number": s.variant_number,
            "answered_count": _count_answered(s.answers),
            "total_tasks": s.total_tasks or exam.tasks_per_variant,
            "score": s.score,
            "max_score": s.max_score,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
        })

    exam_started_at_str = (exam.settings or {}).get("started_at")
    if exam_started_at_str:
        try:
            exam_started_at = _to_naive_utc(datetime.fromisoformat(exam_started_at_str))
            if exam_started_at and (not earliest_started_at or exam_started_at < earliest_started_at):
                earliest_started_at = exam_started_at
        except Exception:
            pass

    total_time_seconds = exam.total_time_minutes * 60
    time_remaining_seconds = total_time_seconds
    time_elapsed_seconds = 0

    if exam.status == "active" and earliest_started_at:
        now_utc = datetime.utcnow()
        time_elapsed_seconds = max(0, int((now_utc - earliest_started_at).total_seconds()))
        time_remaining_seconds = max(0, total_time_seconds - time_elapsed_seconds)
    elif exam.status == "closed":
        time_remaining_seconds = 0
        time_elapsed_seconds = total_time_seconds

    return {
        "exam_id": exam.id,
        "title": exam.title,
        "access_code": exam.access_code if actor.role == "teacher" else None,
        "status": exam.status,
        "group_id": exam.group_id,
        "group_name": exam.group_name,
        "total_time_minutes": exam.total_time_minutes,
        "total_time_seconds": total_time_seconds,
        "time_remaining_seconds": time_remaining_seconds,
        "time_elapsed_seconds": time_elapsed_seconds,
        "tasks_per_variant": exam.tasks_per_variant,
        "connected_count": len([s for s in session_items if s["status"] in ("waiting", "in_progress", "submitted")]),
        "students": session_items,
    }


@router.delete("/exams/{exam_id}/sessions/{session_id}", summary="Удалить ученика из сессии / лобби")
def kick_student_session(
    exam_id: str,
    session_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    exam = _get_exam_for_teacher_operator(db, exam_id, actor, "kick")
    if exam.delivery_mode == "print":
        raise HTTPException(409, "В печатном экзамене нет лобби и электронных сессий учеников")
    session = SessionRepository(db).get_by_id(session_id)
    if not session or session.exam_id != exam_id:
        raise HTTPException(404, "Сессия не найдена")

    if session.status != "waiting":
        raise HTTPException(409, "Удалить можно только ученика, ожидающего старта в лобби")
    db.delete(session)
    db.commit()
    return {"ok": True, "kicked_session_id": session_id}


def _exam_has_results(db: Session, exam_id: str) -> bool:
    """Has any student worked in this exam?  A started or graded attempt is a result; a lobby entrant (``waiting``) is not."""
    return (
        db.query(ExamSession.id)
        .filter(ExamSession.exam_id == exam_id, ExamSession.status != "waiting")
        .first()
        is not None
    )


def _lock_exam_row(db: Session, exam: GeneratedExam) -> GeneratedExam:
    """Re-read the exam under its row lock (``None`` -> 404: a twin request deleted it while this one waited)."""
    locked = (
        db.query(GeneratedExam)
        .filter(GeneratedExam.id == exam.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if locked is None:
        raise HTTPException(404, "Экзамен не найден")
    return locked


@router.delete("/exams/{exam_id}", summary="Удалить экзамен (в архив или навсегда)")
def delete_exam(
    exam_id: str,
    permanent: bool = False,
    reason: Optional[str] = Query(None, max_length=500),
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Archive an exam, or delete an ARCHIVED exam without results for good.

    * ``permanent=false`` archives (draft / variants_ready / closed).  An exam that is already archived stays archived
      and answers 200 again: a double click must never turn the second «archive» into a deletion.
    * ``permanent=true`` is allowed only for an archived exam that has no student results; otherwise 409
      (``EXAM_HAS_RESULTS`` / ``EXAM_NOT_ARCHIVED``).  Every permanent deletion leaves one ``exam_deletions`` row.
    """
    repo = ExamRepository(db)
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    exam = _lock_exam_row(db, exam)
    if exam.status in ("active", "lobby", "print_ready"):
        db.rollback()  # nothing changed: rollback releases the row lock
        raise HTTPException(409, "Сначала завершите экзамен, затем его можно архивировать")
    if permanent:
        if _exam_has_results(db, exam.id):
            db.rollback()  # nothing changed: rollback releases the row lock
            raise CodedHTTPException(
                409,
                "EXAM_HAS_RESULTS",
                "У экзамена есть результаты учеников — его можно только отправить в архив",
            )
        if exam.status != "archived":
            db.rollback()  # nothing changed: rollback releases the row lock
            raise CodedHTTPException(
                409,
                "EXAM_NOT_ARCHIVED",
                "Сначала отправьте экзамен в архив: окончательно удалить можно только архивный экзамен",
            )
        db.add(
            ExamDeletion(
                id=str(uuid4()),
                exam_id=exam.id,
                org_id=str(exam.org_id) if exam.org_id else None,
                title=exam.title,
                deleted_by=actor.user_id,
                reason=(reason or "").strip()[:500] or "Удалён навсегда из архива",
            )
        )
        repo.delete(exam)  # the trace row and the removal are ONE commit
        return {"ok": True, "action": "deleted_permanently", "id": exam_id}
    if exam.status == "archived":
        db.rollback()  # nothing changed: rollback releases the row lock
        return {"ok": True, "action": "archived", "id": exam_id, "already_archived": True}
    exam.status = "archived"
    exam.is_pinned = False
    _record_exam_lifecycle_event(
        db,
        exam,
        {"type": "exam_archived", "group_id": exam.group_id, "group_name": exam.group_name},
        actor_id=actor.user_id,
    )
    db.commit()
    return {"ok": True, "action": "archived", "id": exam_id}


@router.post("/exams/{exam_id}/restore", summary="Восстановить экзамен из архива")
def restore_exam(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Bring an ARCHIVED exam back to a state it can be continued from; any other exam is a 409.

    * an exam that was held (graded sessions, or a completed print package) returns as ``closed``;
    * anything else returns as an editable draft with no leftover lobby code or lesson binding.
    """
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    exam = _lock_exam_row(db, exam)
    if exam.status != "archived":
        db.rollback()  # nothing changed: rollback releases the row lock
        raise CodedHTTPException(409, "EXAM_NOT_ARCHIVED", "Восстановить можно только экзамен из архива")
    held = False
    if exam.delivery_mode == "print":
        package = db.query(ExamPrintPackage).filter(ExamPrintPackage.exam_id == exam.id).first()
        held = bool(package and package.status == "completed")
    elif exam.group_id:
        held = _has_any_sessions(db, exam.id)
    if held:
        exam.status = "closed"
    else:
        # the same reset a cancelled lobby gets: draft/variants_ready, no access code, no lesson occurrence
        _reset_lobby_to_draft(db, exam)
    _record_exam_lifecycle_event(
        db,
        exam,
        {"type": "exam_restored", "group_id": exam.group_id, "group_name": exam.group_name, "details": {"restored_status": exam.status}},
        actor_id=actor.user_id,
    )
    db.commit()
    db.refresh(exam)
    return {"ok": True, "action": "restored", "id": exam_id, "status": exam.status}


@router.get("/exams/{exam_id}/draft", response_model=DraftExamResponse,
            summary="Черновик экзамена с задачами (учитель)")
def get_exam_draft(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)

    _invalidate_unbound_open_exam(db, exam)
    variants_out: List[DraftVariant] = []
    for v in (exam.variants or []):
        tasks_raw = v.get("tasks", [])
        tasks_out = [
            DraftTask(
                task_id=t.get("task_id", ""),
                skill_id=t.get("skill_id", ""),
                skill_name=t.get("skill_name", ""),
                difficulty=t.get("difficulty", "B"),
                question_text=t.get("question_text", t.get("text", "")),
                question_latex=t.get("question_latex"),
                question_image_url=t.get("question_image_url"),
                answer_type=t.get("answer_type", "exact_number"),
                correct_answer=t.get("correct_answer", ""),
                correct_answer_latex=t.get("correct_answer_latex"),
                answer_options=normalize_answer_options(t.get("answer_options")),
                distractor_meta=normalize_distractor_meta(t.get("distractor_meta")),
                is_template_generated=t.get("is_template_generated", False),
            )
            for t in tasks_raw
        ]
        variants_out.append(DraftVariant(
            variant_id=str(v.get("variant_number", 1)),
            variant_number=v.get("variant_number", 1),
            total_points=len(tasks_out) * 10,
            tasks=tasks_out,
        ))
    return DraftExamResponse(
        exam_id=exam.id,
        title=exam.title,
        status=exam.status,
        delivery_mode=exam.delivery_mode or "online",
        # The admin monitoring page is intentionally read-only and never
        # receives a student-entry capability.
        access_code=exam.access_code if actor.role == "teacher" else None,
        total_time_minutes=exam.total_time_minutes,
        variants_count=exam.variants_count,
        tasks_per_variant=exam.tasks_per_variant,
        group_id=exam.group_id,
        group_name=exam.group_name,
        class_level=exam.class_level,
        teacher_name=exam.teacher_name,
        scheduled_lesson_id=exam.scheduled_lesson_id,
        scheduled_lesson_date=exam.scheduled_lesson_date,
        settings=exam.settings or {},
        print_package=_print_package_metadata(db, exam.id),
        skills_covered=exam.skills_covered or [],
        created_at=exam.created_at,
        variants=variants_out,
    )


@router.put(
    "/exams/{exam_id}/print-layout",
    summary="Настроить типы ответов и место для решения в печатном бланке",
)
def update_print_layout(
    exam_id: str,
    req: UpdatePrintLayoutRequest,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Persist only editable print layout; source tasks are never modified."""
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    _require_print_delivery(exam)
    if exam.status not in {"draft", "variants_ready"}:
        raise HTTPException(409, "Настройка бланка доступна только до подготовки печатного комплекта")
    if _has_any_sessions(db, exam.id):
        raise HTTPException(409, "Экзамен уже имеет попытки и не может быть изменён")
    if _print_package_metadata(db, exam.id):
        raise HTTPException(409, "Печатный комплект уже зафиксирован. Для изменений создайте копию экзамена")

    try:
        layout = validate_print_layout(exam.variants or [], req.layout)
    except PrintLayoutValidationError as exc:
        raise HTTPException(422, str(exc)) from exc

    settings = deepcopy(exam.settings or {})
    settings["print_layout"] = layout
    exam.settings = settings
    db.commit()
    return {"exam_id": exam.id, "layout": layout, "status": exam.status}


@router.post(
    "/exams/{exam_id}/print-package/prepare",
    summary="Зафиксировать печатный комплект для конкретного занятия",
)
def prepare_print_package(
    exam_id: str,
    req: PreparePrintPackageRequest,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Create one immutable, lesson-bound printable package.

    The task snapshot and answer key are stored together server-side.  Browser
    data can choose a lesson and a previously validated layout, but it cannot
    alter the final task content, correct answers or package hash.
    """
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    _require_print_delivery(exam)
    if exam.status not in {"draft", "variants_ready"}:
        raise HTTPException(409, "Печатный комплект можно подготовить только из черновика")
    if _has_any_sessions(db, exam.id):
        raise HTTPException(409, "Экзамен уже имеет попытки. Для нового проведения создайте копию")
    if db.query(ExamPrintPackage.id).filter(ExamPrintPackage.exam_id == exam.id).first():
        raise HTTPException(409, "Печатный комплект уже был создан. Для нового занятия создайте копию экзамена")

    canonical_group, _ = _await(_resolve_lesson_occurrence(
        actor,
        group_id=req.group_id,
        lesson_id=req.lesson_id,
        lesson_date=req.lesson_date,
    ), db)
    _ensure_lesson_slot_is_available(
        db,
        exam,
        org_id=actor.org_id,
        lesson_id=req.lesson_id,
        lesson_date=req.lesson_date,
    )
    try:
        layout = validate_print_layout(
            exam.variants or [],
            (exam.settings or {}).get("print_layout") or build_default_print_layout(exam.variants or []),
        )
        snapshot = build_print_snapshot(
            exam_id=exam.id,
            title=exam.title,
            subject=exam.subject or "Математика",
            group_name=canonical_group.get("name"),
            class_level=canonical_group.get("class_level"),
            lesson_id=req.lesson_id,
            lesson_date=req.lesson_date,
            total_time_minutes=exam.total_time_minutes or 45,
            variants=exam.variants or [],
            layout=layout,
        )
    except PrintLayoutValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    snapshot = _await(_embed_print_figures(snapshot), db)  # nothing is written yet: the bridge ends the read transaction

    exam = _lock_exam_for_transition(
        db,
        exam,
        {"draft", "variants_ready"},
        "Печатный комплект можно подготовить только из черновика",
        no_sessions_refusal="Экзамен уже имеет попытки. Для нового проведения создайте копию",
    )
    if db.query(ExamPrintPackage.id).filter(ExamPrintPackage.exam_id == exam.id).first():
        db.rollback()
        raise HTTPException(409, "Печатный комплект уже был создан. Для нового занятия создайте копию экзамена")
    exam.group_id = str(canonical_group["id"])
    exam.group_name = canonical_group.get("name")
    exam.class_level = canonical_group.get("class_level")
    exam.teacher_name = canonical_group.get("teacher_name") or exam.teacher_name
    exam.scheduled_lesson_id = req.lesson_id
    exam.scheduled_lesson_date = req.lesson_date
    exam.access_code = None
    exam.status = "print_ready"
    settings = deepcopy(exam.settings or {})
    settings["print_layout"] = layout
    exam.settings = settings
    package = ExamPrintPackage(
        id=str(uuid4()),
        exam_id=exam.id,
        org_id=actor.org_id,
        version=1,
        status="ready",
        snapshot=snapshot,
        snapshot_sha256=snapshot_sha256(snapshot),
        created_by=actor.user_id,
    )
    db.add(package)
    _record_exam_lifecycle_event(
        db,
        exam,
        {
            "type": "print_package_prepared",
            "group_id": exam.group_id,
            "group_name": exam.group_name,
            "lesson_id": exam.scheduled_lesson_id,
            "lesson_date": exam.scheduled_lesson_date,
            "details": {
                "delivery_mode": "print",
                "package_version": package.version,
                "snapshot_sha256": package.snapshot_sha256,
            },
        },
        actor_id=actor.user_id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _binding_conflict(exc, package_exists_message=True) from exc
    db.refresh(package)
    return {
        "exam_id": exam.id,
        "status": exam.status,
        "group_id": exam.group_id,
        "group_name": exam.group_name,
        "scheduled_lesson_id": exam.scheduled_lesson_id,
        "scheduled_lesson_date": exam.scheduled_lesson_date,
        "print_package": _print_package_metadata(db, exam.id),
    }


@router.post(
    "/exams/{exam_id}/print-package/start",
    summary="Начать подготовленный печатный экзамен",
)
def start_print_exam(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Start only during the bound lesson; printing itself never opens a lobby."""
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    _require_print_delivery(exam)
    if exam.status != "print_ready":
        raise HTTPException(409, "Начать можно только подготовленный печатный экзамен")
    package = _get_print_package_or_404(db, exam.id)
    if package.status != "ready":
        raise HTTPException(409, "Печатный комплект недоступен для проведения")
    _await(_require_lesson_in_progress(_exam_view(exam), actor), db)
    exam = _lock_exam_for_transition(db, exam, {"print_ready"}, "Печатный экзамен уже начат или больше не готов к началу")
    package = _get_print_package_or_404(db, exam.id)
    if package.status != "ready":
        db.rollback()
        raise HTTPException(409, "Печатный комплект недоступен для проведения")

    exam.status = "active"
    settings = deepcopy(exam.settings or {})
    settings["started_at"] = datetime.utcnow().isoformat()
    exam.settings = settings
    package.status = "released"
    package.released_at = datetime.utcnow()
    _record_exam_lifecycle_event(
        db,
        exam,
        {
            "type": "print_exam_started",
            "group_id": exam.group_id,
            "group_name": exam.group_name,
            "lesson_id": exam.scheduled_lesson_id,
            "lesson_date": exam.scheduled_lesson_date,
            "details": {"delivery_mode": "print", "package_version": package.version},
        },
        actor_id=actor.user_id,
    )
    db.commit()
    return {"exam_id": exam.id, "status": exam.status, "print_package": _print_package_metadata(db, exam.id)}


@router.get("/exams/{exam_id}/print-package", summary="Статус печатного комплекта")
def get_print_package(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    exam = _get_exam_for_actor(db, exam_id, actor)
    _require_print_delivery(exam)
    _get_print_package_or_404(db, exam.id)
    return {"exam_id": exam.id, "status": exam.status, "print_package": _print_package_metadata(db, exam.id)}


def _pdf_response(content: bytes, filename: str, *, download: bool) -> Response:
    disposition = "attachment" if download else "inline"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


def _require_snapshot_variant(snapshot: dict[str, Any], variant_number: int) -> None:
    """A PDF of a variant that does not exist is a plain 404, decided before any rendering work starts."""
    numbers = set()
    for variant in (snapshot or {}).get("variants") or []:
        try:
            numbers.add(int(variant.get("variant_number", 0)))
        except (AttributeError, TypeError, ValueError):
            continue
    if variant_number not in numbers:
        raise HTTPException(404, f"Вариант №{variant_number} в этом экзамене не найден")


def _pdf_download_response(content: bytes, filename: str) -> Response:
    return _pdf_response(content, filename, download=True)


@router.get(
    "/exams/{exam_id}/print-preview/{kind}/{variant_number}.pdf",
    summary="Предпросмотр печатного бланка до привязки к занятию",
)
def preview_print_pdf(
    exam_id: str,
    kind: Literal["student", "teacher-key"],
    variant_number: int,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_heavy_staff),
):
    """Render a staff-only, non-persistent preview from the current draft.

    The endpoint cannot create a package, assign a lesson, publish a public
    code, or open a lobby.  It is deliberately available only to the teacher
    responsible for this draft, because the teacher-key preview contains the
    correct answers.
    """
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    _require_print_delivery(exam)
    if exam.status not in {"draft", "variants_ready"}:
        raise HTTPException(409, "Предпросмотр доступен только до подготовки печатного комплекта")
    if _print_package_metadata(db, exam.id):
        raise HTTPException(409, "Печатный комплект уже зафиксирован. Скачайте его из карточки экзамена")

    snapshot = _build_print_preview_snapshot(exam)
    _require_snapshot_variant(snapshot, variant_number)
    snapshot = _await(_embed_print_figures(snapshot), db)  # ends the read transaction before the figures are fetched
    try:
        pdf = (
            render_student_exam_pdf(snapshot, variant_number)
            if kind == "student"
            else render_teacher_key_pdf(snapshot, variant_number)
        )
    except (KeyError, ValueError) as exc:
        log.exception("Cannot render print preview for exam %s", exam_id)
        raise HTTPException(500, "Не удалось сформировать предпросмотр печатного бланка") from exc

    filename = (
        f"algo_exam_preview_{exam_id}_variant_{variant_number}.pdf"
        if kind == "student"
        else f"algo_exam_preview_key_{exam_id}_variant_{variant_number}.pdf"
    )
    return _pdf_response(pdf, filename, download=False)


@router.get("/exams/{exam_id}/print-package/student/{variant_number}.pdf", summary="Скачать бланк для ученика")
def download_print_student_pdf(
    exam_id: str,
    variant_number: int,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_heavy_staff),
):
    exam = _get_exam_for_actor(db, exam_id, actor)
    _require_print_delivery(exam)
    package = _get_print_package_or_404(db, exam.id)
    if package.status not in {"ready", "released", "completed"}:
        raise HTTPException(410, "Печатный комплект отменён и больше не доступен")
    snapshot, package_id = package.snapshot, package.id
    _require_snapshot_variant(snapshot, variant_number)
    _release_read_transaction(db)  # the rendering below (chromium) takes seconds: no «idle in transaction» meanwhile
    try:
        pdf = render_student_exam_pdf(snapshot, variant_number)
    except (KeyError, ValueError) as exc:
        log.exception("Cannot render student print package %s", package_id)
        raise HTTPException(500, "Не удалось сформировать печатный бланк") from exc
    return _pdf_download_response(pdf, f"algo_exam_{exam_id}_variant_{variant_number}.pdf")


@router.get("/exams/{exam_id}/print-package/teacher-key/{variant_number}.pdf", summary="Скачать ключ преподавателя")
def download_print_teacher_key_pdf(
    exam_id: str,
    variant_number: int,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_heavy_staff),
):
    exam = _get_exam_for_actor(db, exam_id, actor)
    _require_print_delivery(exam)
    if actor.role != "teacher" and exam.status not in {"closed", "archived"}:
        raise HTTPException(403, "Ключ доступен администратору только после завершения экзамена")
    package = _get_print_package_or_404(db, exam.id)
    if package.status not in {"ready", "released", "completed"}:
        raise HTTPException(410, "Печатный комплект отменён и больше не доступен")
    snapshot, package_id = package.snapshot, package.id
    _require_snapshot_variant(snapshot, variant_number)
    _release_read_transaction(db)  # the rendering below (chromium) takes seconds: no «idle in transaction» meanwhile
    try:
        pdf = render_teacher_key_pdf(snapshot, variant_number)
    except (KeyError, ValueError) as exc:
        log.exception("Cannot render teacher print key %s", package_id)
        raise HTTPException(500, "Не удалось сформировать ключ преподавателя") from exc
    return _pdf_download_response(pdf, f"algo_exam_{exam_id}_teacher_key_variant_{variant_number}.pdf")


@router.get(
    "/admin/exams/{exam_id}/view",
    response_model=AdminExamViewResponse,
    summary="Read-only профиль экзамена для администратора",
)
def get_admin_exam_view(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Return the same academic snapshot as the teacher view, without controls.

    This is intentionally a separate contract from ``/draft``.  A browser
    cannot turn an administrator into an operator by changing a route, and a
    live answer key is not returned before the run is closed.
    """
    _require_exam_admin_observer(actor)
    exam = _get_exam_for_actor(db, exam_id, actor)
    _check_and_auto_close_exam(db, exam)

    answer_key_available = exam.status in {"closed", "archived"}
    variants: list[AdminExamVariant] = []
    for variant in exam.variants or []:
        task_rows: list[AdminExamTask] = []
        for task in variant.get("tasks", []):
            task_rows.append(
                AdminExamTask(
                    task_id=task.get("task_id", ""),
                    skill_id=task.get("skill_id", ""),
                    skill_name=task.get("skill_name", ""),
                    difficulty=task.get("difficulty", "B"),
                    question_text=task.get("question_text", task.get("text", "")),
                    question_latex=task.get("question_latex"),
                    question_image_url=task.get("question_image_url"),
                    answer_type=task.get("answer_type", "exact_number"),
                    answer_options=normalize_answer_options(task.get("answer_options")),
                    correct_answer=task.get("correct_answer", "") if answer_key_available else None,
                    correct_answer_latex=task.get("correct_answer_latex") if answer_key_available else None,
                    distractor_meta=normalize_distractor_meta(task.get("distractor_meta")) if answer_key_available else None,
                )
            )
        variants.append(
            AdminExamVariant(
                variant_id=str(variant.get("variant_number", 1)),
                variant_number=variant.get("variant_number", 1),
                total_points=len(task_rows) * 10,
                tasks=task_rows,
            )
        )

    submitted_count = (
        db.query(ExamSession)
        .filter(ExamSession.exam_id == exam.id, ExamSession.status == "submitted")
        .count()
    )
    summary = _monitor_summary(db, exam, submitted_count)
    return AdminExamViewResponse(
        **summary.model_dump(),
        answer_key_available=answer_key_available,
        variants=variants,
        lifecycle_events=_recent_exam_lifecycle_events(db, exam),
    )


@router.get("/exams/{exam_id}/draft/alternatives",
            summary="Альтернативные задачи для замены (учитель)")
def get_task_alternatives(
    exam_id: str,
    variant_number: int = Query(1),
    task_id: str = Query(...),
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    variant = next(
        (v for v in (exam.variants or []) if v.get("variant_number") == variant_number),
        None,
    )
    if not variant:
        raise HTTPException(404, "Вариант не найден")
    old_task = next(
        (t for t in variant.get("tasks", []) if t.get("task_id") == task_id),
        None,
    )
    if not old_task:
        raise HTTPException(404, "Задача не найдена")
    used_ids = [t.get("task_id") for t in variant.get("tasks", [])]
    skill_ids = [old_task["skill_id"]] if old_task.get("skill_id") else []
    task_repo = TaskRepository()
    alts = (
        task_repo.get_tasks_by_skills(skill_ids, None, 5, used_ids)
        if skill_ids
        else []
    )
    return alts


@router.post("/exams/{exam_id}/draft/replace-task",
             summary="Заменить задачу в черновике (учитель)")
def replace_task(
    exam_id: str,
    req: ReplaceTaskRequest,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    import copy
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    if exam.status not in ("draft", "variants_ready"):
        raise HTTPException(400, "Замена задач возможна только в черновике")

    new_variants = copy.deepcopy(exam.variants or [])
    variant = next(
        (v for v in new_variants if v.get("variant_number") == req.variant_number),
        None,
    )
    if not variant:
        raise HTTPException(404, "Вариант не найден")

    target_old_id = req.old_task_id or req.task_id
    old_task = next(
        (t for t in variant.get("tasks", []) if t.get("task_id") == target_old_id),
        None,
    )
    if not old_task and req.new_task_id and target_old_id != req.task_id:
        old_task = next(
            (t for t in variant.get("tasks", []) if t.get("task_id") == req.task_id),
            None,
        )
        if old_task:
            target_old_id = req.task_id

    if not old_task:
        raise HTTPException(404, "Задача для замены не найдена в варианте")

    task_repo = TaskRepository()
    if req.new_task_id:
        new_task_raw = task_repo.get_task_by_id(req.new_task_id)
        if not new_task_raw:
            raise HTTPException(404, "Выбранная задача не найдена в базе")
        new_task = _normalize_content_task(new_task_raw)
        # The same task twice in one variant is silently mis-graded (verdicts are keyed by task_id, the maximum
        # counts both) and breaks the print layout: refuse it.  Replacing a task by itself is a harmless no-op.
        other_ids = {
            str(t.get("task_id")) for t in variant.get("tasks", []) if t.get("task_id") != target_old_id
        }
        if {str(req.new_task_id), str(new_task.get("task_id"))} & other_ids:
            raise HTTPException(409, "Эта задача уже есть в варианте. Выберите другую для замены")
    else:
        used_ids = [t.get("task_id") for t in variant.get("tasks", [])]
        skill_ids = [old_task["skill_id"]] if old_task.get("skill_id") else []
        alternatives = (
            task_repo.get_tasks_by_skills(skill_ids, old_task.get("difficulty"), 1, used_ids)
            if skill_ids else []
        )
        if not alternatives and skill_ids:
            alternatives = task_repo.get_tasks_by_skills(skill_ids, None, 1, used_ids)
        if not alternatives and old_task.get("toc_id"):
            alternatives = task_repo.get_tasks_by_toc_ids([old_task["toc_id"]], old_task.get("difficulty"), 1, used_ids)
        if not alternatives and old_task.get("toc_id"):
            alternatives = task_repo.get_tasks_by_toc_ids([old_task["toc_id"]], None, 1, used_ids)
        if not alternatives and exam.toc_ids_used:
            alternatives = task_repo.get_tasks_by_toc_ids(list(exam.toc_ids_used), None, 1, used_ids)
        if not alternatives:
            raise HTTPException(400, "Нет доступных задач для замены")
        new_task = _normalize_content_task(alternatives[0])

    variant["tasks"] = [
        new_task if t.get("task_id") == target_old_id else t
        for t in variant.get("tasks", [])
    ]
    exam.variants = new_variants
    if exam.delivery_mode == "print":
        settings = copy.deepcopy(exam.settings or {})
        settings["print_layout"] = reconcile_print_layout(
            new_variants,
            settings.get("print_layout"),
        )
        exam.settings = settings
    db.add(exam)
    db.commit()
    db.refresh(exam)
    return {"status": "replaced", "new_task": new_task}


@router.get("/exams/{exam_id}", response_model=ExamSummaryResponse,
            summary="Информация об экзамене (учитель)")
def get_exam(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    exam = _get_exam_for_actor(db, exam_id, actor, "get")

    # Check and auto-close if exam duration expired
    _check_and_auto_close_exam(db, exam)

    return ExamSummaryResponse(
        exam_id=exam.id,
        title=exam.title,
        status=exam.status,
        delivery_mode=exam.delivery_mode or "online",
        access_code=exam.access_code if actor.role == "teacher" else None,
        tasks_per_variant=exam.tasks_per_variant,
        variants_count=exam.variants_count,
        group_id=exam.group_id,
        group_name=exam.group_name,
        class_level=exam.class_level,
        teacher_name=exam.teacher_name,
        scheduled_lesson_id=exam.scheduled_lesson_id,
        scheduled_lesson_date=exam.scheduled_lesson_date,
        recent_events=_recent_exam_lifecycle_events(db, exam),
        total_time_minutes=exam.total_time_minutes or 45,
        created_at=exam.created_at,
    )


# ═══════════════════════════════════════════════════════════════════════════
# STUDENT endpoints (public)
# ═══════════════════════════════════════════════════════════════════════════


def _detached_exam_copy(exam: GeneratedExam, *, skip: tuple = ()) -> GeneratedExam:
    """A snapshot of an exam row that belongs to NO session (``skip``: columns left out, e.g. the task JSON).

    The public entry looks at the exam, then talks to auth-service, then writes.  Nothing may be held open across the
    network call - not a row lock, not even a pooled connection sitting «idle in transaction» - so the first phase
    copies the columns it needs, ends its transaction, and the awaiting helpers read the copy.  (``expunge`` would
    detach the object from a session other code may still use; a copy touches nothing.)
    """
    return GeneratedExam(
        **{attr.key: getattr(exam, attr.key) for attr in sa_inspect(GeneratedExam).column_attrs if attr.key not in skip}
    )


def _exam_view(exam: GeneratedExam) -> GeneratedExam:
    """What a coroutine handed to ``_await`` may read: a copy, so it never triggers a lazy load on the event loop."""
    return _detached_exam_copy(exam, skip=("variants",))


def _entry_exam_lookup(db: Session, code: str) -> GeneratedExam:
    """Phase 1 of a public entry: the exam of a code, as a snapshot.  Read-only, the transaction ends here."""
    exam = ExamRepository(db).get_by_access_code(code, without_tasks=True)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    if exam.delivery_mode == "print":
        raise HTTPException(410, "Этот экзамен проводится по печатным бланкам в аудитории.")
    snapshot = _detached_exam_copy(exam, skip=("variants",))
    db.commit()
    return snapshot


def _entry_gate(db: Session, exam_id: str) -> GeneratedExam:
    """What check-student and start verify about the exam BEFORE they call auth-service; a snapshot comes out.

    Closing a timed-out exam (the only write) happens here, in its own transaction; a timer that has not run out
    takes no lock.  The transaction ends before the caller awaits anything.
    """
    exam = ExamRepository(db).get_by_id(exam_id, without_tasks=True)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    _check_and_auto_close_exam(db, exam)
    if exam.status not in ("active", "lobby"):
        raise HTTPException(410, "Экзамен уже завершён. Повторное прохождение недоступно.")
    try:
        _require_bound_exam(exam)
    except HTTPException as exc:
        raise HTTPException(410, "Экзамен ещё не назначен на занятие") from exc
    snapshot = _detached_exam_copy(exam, skip=("variants",))
    db.commit()
    return snapshot


async def _close_ticket(ticket: EntryTicket) -> None:
    """Release the in-flight reservation.  Shielded: a client that hangs up must not keep its slot taken."""
    with anyio.CancelScope(shield=True):
        await run_in_threadpool(ticket.close)


def _public_exam_precheck(db: Session, code: str) -> GeneratedExam:
    exam = ExamRepository(db).get_by_access_code(code, without_tasks=True)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")

    # A print package is deliberately never reachable through the public
    # online-exam flow.  ``access_code`` is normally NULL for print mode, but
    # this guard also protects historical/imported rows from accidentally
    # exposing tasks if such a code exists.
    if exam.delivery_mode == "print":
        raise HTTPException(410, "Этот экзамен проводится по печатным бланкам в аудитории.")

    # Check and auto-close if time expired
    _check_and_auto_close_exam(db, exam)

    # A historical completed exam may predate the lesson-binding workflow.
    # Its old public URL must still communicate the meaningful terminal state,
    # rather than incorrectly implying that a teacher needs to assign it now.
    if exam.status not in ("active", "lobby"):
        raise HTTPException(410, "Экзамен уже завершён. Повторное прохождение недоступно.")

    try:
        _require_bound_exam(exam)
    except HTTPException as exc:
        if exc.status_code == 409:
            raise HTTPException(410, "Экзамен ещё не назначен на занятие") from exc
        raise
    snapshot = _detached_exam_copy(exam, skip=("variants",))
    db.commit()
    return snapshot


def _exam_info_response(db: Session, exam: GeneratedExam) -> ExamInfoResponse:
    remaining_seconds = None
    started_at_str = None
    if exam.status == "active":
        exam_start = None
        s_str = (exam.settings or {}).get("started_at")
        if s_str:
            try:
                exam_start = _to_naive_utc(datetime.fromisoformat(s_str))
            except Exception:
                pass
        if not exam_start:
            sessions = SessionRepository(db).list_by_exam(exam.id)
            for s in sessions:
                if s.started_at:
                    s_naive = _to_naive_utc(s.started_at)
                    if s_naive and (not exam_start or s_naive < exam_start):
                        exam_start = s_naive

        total_sec = (exam.total_time_minutes or 45) * 60
        if exam_start:
            started_at_str = exam_start.isoformat()
            now_utc = datetime.utcnow()
            elapsed = max(0, int((now_utc - exam_start).total_seconds()))
            remaining_seconds = max(0, total_sec - elapsed)
        else:
            remaining_seconds = total_sec

    return ExamInfoResponse(
        exam_id=exam.id,
        title=exam.title,
        subject=exam.subject or "Математика",
        teacher_name=exam.teacher_name,
        group_id=exam.group_id,
        group_name=exam.group_name,
        class_level=exam.class_level,
        total_time_minutes=exam.total_time_minutes,
        tasks_per_variant=exam.tasks_per_variant,
        variants_count=exam.variants_count,
        status=exam.status,
        time_remaining_seconds=remaining_seconds,
        started_at=started_at_str,
    )


@router.get("/exams/by-code/{code}", response_model=ExamInfoResponse,
            summary="Информация об экзамене по коду (студент)")
async def get_exam_by_code(code: str, db: Session = Depends(get_db)):
    # Database work runs in the thread pool and its transaction is over before the network call (ROADMAP #95).
    exam = await run_in_threadpool(_public_exam_precheck, db, code)
    if exam.status == "lobby":
        await _require_public_lobby_available(exam)
    return await run_in_threadpool(_exam_info_response, db, exam)


def _norm_ident(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _find_student_session(
    sessions: list,
    statuses: tuple,
    student_id: Optional[str],
    identifier: Optional[str] = None,
):
    """Find the attempt that belongs to ONE canonical roster student.

    Identity only, never a name: two students of a group may share a full name
    ("Мария Иванова" TE-040 and TE-041), and a name is no evidence that two
    people are the same.  A session matches when its ``student_id`` (the
    canonical UUID) or its ``display_id`` equals the canonical UUID or the
    display ID of the student.  Older rows that kept the display ID in
    ``student_id`` still match.  A session with neither (or with a different
    one) matches nothing, whatever name it carries - it fails closed.
    """
    keys = {key for key in (_norm_ident(student_id), _norm_ident(identifier)) if key}
    if not keys:
        return None
    for s in sessions:
        if s.status not in statuses:
            continue
        if _norm_ident(s.student_id) in keys or _norm_ident(s.display_id) in keys:
            return s
    return None


def _find_submitted_session(sessions: list, student_id: Optional[str], identifier: Optional[str] = None):
    """Find if a student already completed an exam session."""
    return _find_student_session(sessions, ("submitted",), student_id, identifier)


def _find_active_session(sessions: list, student_id: Optional[str], identifier: Optional[str] = None):
    """Find if a student currently has a waiting or in_progress session."""
    return _find_student_session(sessions, ("waiting", "in_progress"), student_id, identifier)


@router.post("/exams/by-code/{code}/check-student", response_model=CheckStudentExamResponse,
             summary="Проверить принадлежность ученика к группе экзамена и статус попытки")
async def check_student_for_exam(
    code: str,
    req: CheckStudentExamRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    exam = await run_in_threadpool(_entry_exam_lookup, db, code)
    # #33: the answer carries the student's name on a match, so every lookup is rate limited (429 + Retry-After)
    # per exam code and client address; a distinct failed input is what spends the strict budget.
    ticket = await run_in_threadpool(
        admit_identity_attempt, request, exam.access_code or code, req.student_id, req.student_name, req.identifier
    )
    try:
        return await _check_student_admitted(exam, req, ticket, db)
    finally:
        await _close_ticket(ticket)  # releases the in-flight reservation (a real failure was counted by ticket.fail())


def _check_student_outcome(db: Session, exam: GeneratedExam, student: dict) -> CheckStudentExamResponse:
    all_sessions = SessionRepository(db).list_by_exam(exam.id)
    submitted_sess = _find_submitted_session(
        all_sessions,
        student.get("id"),
        student.get("display_id"),
    )
    if submitted_sess:
        response = CheckStudentExamResponse(
            enrolled=False,
            group_required=True,
            group_name=exam.group_name,
            student=student,
            already_submitted=True,
            session_id=submitted_sess.id,
            message="Вы уже завершили этот экзамен. Повторное прохождение недоступно.",
        )
    else:
        response = CheckStudentExamResponse(
            enrolled=True,
            group_required=True,
            group_name=exam.group_name,
            student=student,
        )
    db.commit()
    return response


async def _check_student_admitted(
    exam: GeneratedExam,
    req: CheckStudentExamRequest,
    ticket: EntryTicket,
    db: Session,
) -> CheckStudentExamResponse:
    # Three phases, never a transaction across a network call: (1) database, in a thread, ends its transaction;
    # (2) auth-service; (3) database again.  See _detached_exam_copy.
    exam = await run_in_threadpool(_entry_gate, db, exam.id)
    if exam.status == "lobby":
        await _require_public_lobby_available(exam)

    try:
        student = await _canonical_student_for_exam(
            exam,
            student_id=req.student_id,
            student_name=req.student_name,
            identifier=req.identifier,
        )
    except HTTPException as exc:
        if exc.status_code == 403:
            await run_in_threadpool(ticket.fail)
            return CheckStudentExamResponse(
                enrolled=False,
                group_required=True,
                group_name=exam.group_name,
                student=None,
                message=str(exc.detail),
            )
        raise
    if student.pop("_resume_only", False) and not await run_in_threadpool(_has_resumable_session, db, exam.id, student):
        await run_in_threadpool(ticket.fail)
        return CheckStudentExamResponse(
            enrolled=False, group_required=True, group_name=exam.group_name, student=None, message=STUDENT_NOT_FOUND_MESSAGE
        )
    await run_in_threadpool(ticket.matched, student.get("id"))
    return await run_in_threadpool(_check_student_outcome, db, exam, student)


@router.post("/exams/by-code/{code}/start", response_model=StartExamResponse,
             summary="Начать экзамен или войти в лобби (студент)")
async def start_exam(
    code: str,
    req: StartExamRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    exam = await run_in_threadpool(_entry_exam_lookup, db, code)
    # #33: the same budget as check-student
    ticket = await run_in_threadpool(
        admit_identity_attempt, request, exam.access_code or code, req.student_id or req.display_id, req.student_name,
        req.display_id,
    )
    try:
        return await _start_exam_admitted(exam, req, ticket, db)
    finally:
        await _close_ticket(ticket)


def _admit_student_to_exam(
    db: Session,
    exam_id: str,
    req: StartExamRequest,
    canonical_student: dict,
    resume_only: bool = False,
) -> StartExamResponse:
    """The transactional core of ``start``: no network call, the whole thing runs in one thread.

    A SHARED lock on the exam row (FOR SHARE) is taken first and held only until the session row is committed: it
    keeps a finalize (FOR UPDATE, which flips the exam to «closed» and grades every attempt it can see) from running
    between the status check and the INSERT - otherwise an attempt could appear in an already closed exam - while
    students entering at the same moment do not wait for each other.
    """
    canonical_student_id = str(canonical_student["id"])
    canonical_display_id = canonical_student.get("display_id")
    canonical_student_name = canonical_student.get("full_name") or req.student_name
    canonical_class_level = canonical_student.get("class_level") or req.class_level

    exam = (
        db.query(GeneratedExam)
        .filter(GeneratedExam.id == exam_id)
        .with_for_update(read=True)
        .populate_existing()
        .first()
    )
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    if exam.status not in ("active", "lobby"):  # closed between the gate and now (teacher, or the common timer)
        raise HTTPException(410, "Экзамен уже завершён. Повторное прохождение недоступно.")
    view = _detached_exam_copy(exam)  # what the response is built from, whatever the commits below expire
    variants = view.variants or []
    if not variants:
        raise HTTPException(500, "Экзамен не содержит вариантов")
    exam_is_lobby = view.status == "lobby"

    all_sessions = SessionRepository(db).list_by_exam(exam_id)

    # 1. Single attempt guard: Check if already submitted
    submitted_sess = _find_submitted_session(
        all_sessions, canonical_student_id, canonical_display_id
    )
    if submitted_sess:
        response = StartExamResponse(
            session_id=submitted_sess.id,
            variant_number=submitted_sess.variant_number,
            time_minutes=view.total_time_minutes,
            time_remaining_seconds=0,
            tasks=[],
            status="submitted",
            already_submitted=True,
            message="Вы уже завершили этот экзамен. Повторное прохождение недоступно.",
        )
        db.commit()
        return response

    # 2. Check if student already has a session in progress or waiting
    existing_session = _find_active_session(
        all_sessions, canonical_student_id, canonical_display_id
    )
    if resume_only and (existing_session is None or existing_session.status != "in_progress"):
        # O17: a student who is no longer in the group may only resume the attempt he had; never open a new one
        raise HTTPException(403, STUDENT_NOT_FOUND_MESSAGE)

    # Determine global exam start time
    exam_start_time = None
    exam_started_at_str = (view.settings or {}).get("started_at")
    if exam_started_at_str:
        try:
            exam_start_time = _to_naive_utc(datetime.fromisoformat(exam_started_at_str))
        except Exception:
            pass

    now_utc = datetime.utcnow()

    if existing_session:
        session = existing_session
        if not session.display_id and canonical_display_id:
            session.display_id = canonical_display_id
            db.commit()
    else:
        variant = random.choice(variants)
        tasks_raw = variant.get("tasks", [])
        session_id = f"SES_{uuid4().hex[:16]}"
        initial_status = "waiting" if exam_is_lobby else "in_progress"
        session = ExamSession(
            id=session_id,
            exam_id=exam_id,
            student_id=canonical_student_id,
            display_id=canonical_display_id,
            student_name=canonical_student_name,
            class_level=canonical_class_level,
            session_token=_generate_session_token(),
            variant_number=variant.get("variant_number", 1),
            answers={},
            max_score=len(tasks_raw) * 10,
            status=initial_status,
            started_at=(exam_start_time or now_utc) if initial_status == "in_progress" else None,
        )
        try:
            SessionRepository(db).create(session)
        except IntegrityError:
            # The DB uniqueness rule wins a race between two browser tabs.
            db.rollback()
            session = (
                db.query(ExamSession)
                .filter(ExamSession.exam_id == exam_id, ExamSession.student_id == canonical_student_id)
                .first()
            )
            if not session:
                raise HTTPException(409, "Не удалось создать единственную попытку ученика")

    if not session.session_token:
        # Legacy active session: issue a capability only after its identity was
        # checked again against the live roster above.
        session.session_token = _generate_session_token()
        db.commit()

    # If session is waiting in lobby, do not return tasks yet
    if exam_is_lobby or session.status == "waiting":
        response = StartExamResponse(
            session_id=session.id,
            variant_number=session.variant_number,
            time_minutes=view.total_time_minutes,
            time_remaining_seconds=view.total_time_minutes * 60,
            tasks=[],
            status="waiting",
            saved_answers=session.answers or {},
            session_token=session.session_token,
        )
        db.commit()
        return response

    # If exam is active and session is in_progress, calculate remaining time based on exam start time
    if not session.started_at:
        session.started_at = exam_start_time or now_utc
        db.commit()

    total_seconds = (view.total_time_minutes or 45) * 60
    base_start = exam_start_time or _to_naive_utc(session.started_at) or now_utc
    elapsed_seconds = max(0, int((now_utc - base_start).total_seconds()))
    remaining_seconds = max(0, total_seconds - elapsed_seconds)

    # If time has expired, auto-submit session and auto-close exam
    if remaining_seconds <= 0:
        # The attempt row is locked while it is graded: a save from a second tab must not interleave.  (The shared
        # exam lock is gone with the commit above or just below - the finalizer needs the exclusive one.)
        session_id_to_grade = session.id
        db.commit()
        locked = (
            db.query(ExamSession)
            .filter(ExamSession.id == session_id_to_grade)
            .with_for_update()
            .populate_existing()
            .first()
        )
        if locked is None:  # the teacher removed the attempt while auth-service was answering
            raise HTTPException(404, "Сессия не найдена")
        if locked.status in ("in_progress", "waiting"):
            _grade_and_submit_session(db, locked, view, locked.answers or {}, total_seconds)
        else:
            db.commit()
        _check_and_auto_close_exam(db, exam)
        session = locked
        return StartExamResponse(
            session_id=session.id,
            variant_number=session.variant_number,
            time_minutes=view.total_time_minutes,
            time_remaining_seconds=0,
            tasks=[],
            status="submitted",
            already_submitted=True,
            saved_answers=session.answers or {},
            message="Время экзамена истекло. Ваши сохранённые ответы отправлены на проверку.",
        )

    # Build tasks for the student's assigned variant
    variant_idx = max(0, min(session.variant_number - 1, len(variants) - 1))
    variant = variants[variant_idx]
    tasks_raw = variant.get("tasks", [])
    mc_mode = (view.settings or {}).get("mc_mode", "auto")

    tasks_public = []
    for t in tasks_raw:
        show_mc = _should_show_mc(t, mc_mode)
        options = _build_session_mc_options(view, session, t) if show_mc else []
        answer_type = t.get("answer_type", "exact_number")
        if show_mc and options:
            answer_type = "multiple_choice"
        elif answer_type == "multiple_choice" and not options:
            answer_type = "exact_number"

        tasks_public.append(ExamTaskPublic(
            task_id=t.get("task_id", ""),
            skill_id=t.get("skill_id", ""),
            skill_name=t.get("skill_name", ""),
            difficulty=t.get("difficulty", "B"),
            text=t.get("question_text", t.get("text", "")),
            question_latex=t.get("question_latex"),
            options=options,
            options_latex=_build_mc_options_latex(t, options) if options else None,
            image_url=t.get("question_image_url"),
            answer_type=answer_type,
            points=10,
        ))

    response = StartExamResponse(
        session_id=session.id,
        variant_number=session.variant_number,
        time_minutes=view.total_time_minutes,
        time_remaining_seconds=remaining_seconds,
        tasks=tasks_public,
        status="in_progress",
        saved_answers=session.answers or {},
        session_token=session.session_token,
    )
    db.commit()  # ends the read transaction (and with it the shared exam lock if no write above already did)
    return response


async def _start_exam_admitted(
    exam: GeneratedExam,
    req: StartExamRequest,
    ticket: EntryTicket,
    db: Session,
) -> StartExamResponse:
    # (1) database, in a thread: close a timed-out exam, check the state; the transaction ends before (2)
    exam = await run_in_threadpool(_entry_gate, db, exam.id)
    if exam.status == "lobby":
        await _require_public_lobby_available(exam)

    # (2) auth-service: no transaction, no lock, no pooled connection is held while we wait for it
    try:
        canonical_student = await _canonical_student_for_exam(
            exam,
            student_id=req.student_id or req.display_id,
            student_name=req.student_name,
            identifier=req.display_id,
        )
    except HTTPException as exc:
        if exc.status_code == 403:
            await run_in_threadpool(ticket.fail)
        raise
    resume_only = bool(canonical_student.pop("_resume_only", False))
    if resume_only and not await run_in_threadpool(_has_resumable_session, db, exam.id, canonical_student):
        await run_in_threadpool(ticket.fail)
        raise HTTPException(403, STUDENT_NOT_FOUND_MESSAGE)
    await run_in_threadpool(ticket.matched, canonical_student.get("id"))

    # (3) database again, in a thread: one short transaction
    return await run_in_threadpool(_admit_student_to_exam, db, exam.id, req, canonical_student, resume_only)


@router.get("/exams/by-code/{code}/session-status/{session_id}",
            summary="Проверка статуса сессии / ожидание старта в лобби")
def get_session_status(
    code: str,
    session_id: str,
    db: Session = Depends(get_db),
    session_token: Optional[str] = Header(None, alias="X-Exam-Session-Token"),
):
    exam = ExamRepository(db).get_by_access_code(code)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    if exam.delivery_mode == "print":
        raise HTTPException(410, "Этот экзамен проводится по печатным бланкам в аудитории.")
    _check_and_auto_close_exam(db, exam)
    session = SessionRepository(db).get_by_id(session_id)
    if not session or session.exam_id != exam.id:
        return {"status": "kicked", "message": "Сессия не найдена или удалена преподавателем"}
    _require_session_capability(session, session_token)

    # A caller must own the attempt before this endpoint can trigger any state
    # transition (including timeout finalisation).
    if exam.status == "closed":
        # A session that was finalised by the common exam timer is a completed
        # student attempt, not merely a closed waiting room.  Returning this
        # distinction lets the public UI open the student's saved result.
        if session.status == "submitted":
            return {
                "status": "submitted",
                "already_submitted": True,
                "message": "Экзамен завершён. Ваши сохранённые ответы отправлены на проверку.",
            }
        return {"status": "closed", "message": "Экзамен завершён преподавателем до начала вашей попытки"}

    try:
        _require_bound_exam(exam)
    except HTTPException as exc:
        raise HTTPException(410, "Экзамен ещё не назначен на занятие") from exc

    if exam.status == "active":
        now_utc = datetime.utcnow()
        exam_start_time = None
        exam_started_at_str = (exam.settings or {}).get("started_at")
        if exam_started_at_str:
            try:
                exam_start_time = _to_naive_utc(datetime.fromisoformat(exam_started_at_str))
            except Exception:
                pass

        if session.status == "waiting":
            session.status = "in_progress"
            session.started_at = exam_start_time or now_utc
            db.commit()

        # Check remaining time based on synchronized exam start time
        base_start = exam_start_time or _to_naive_utc(session.started_at) or now_utc
        elapsed_sec = max(0, int((now_utc - base_start).total_seconds()))
        total_sec = (exam.total_time_minutes or 45) * 60
        remaining_sec = max(0, total_sec - elapsed_sec)

        # If time expired while waiting/reconnecting
        if remaining_sec <= 0:
            # the attempt row is locked while it is graded (a save of a second tab must not interleave); the grading
            # commits, so the lock is gone before the finalizer takes its exam -> sessions locks
            locked = _lock_attempt(db, session.id)
            if locked is not None and locked.status == "in_progress":
                _grade_and_submit_session(db, locked, exam, locked.answers or {}, total_sec)
            else:
                db.commit()
            _check_and_auto_close_exam(db, exam)
            return {
                "status": "submitted",
                "already_submitted": True,
                "message": "Время экзамена истекло",
            }

        # Build public tasks
        variant_idx = max(0, min(session.variant_number - 1, len(exam.variants or []) - 1))
        variant = (exam.variants or [{}])[variant_idx]
        tasks_raw = variant.get("tasks", [])
        mc_mode = (exam.settings or {}).get("mc_mode", "auto")

        tasks_public = []
        for t in tasks_raw:
            show_mc = _should_show_mc(t, mc_mode)
            options = _build_session_mc_options(exam, session, t) if show_mc else []
            answer_type = t.get("answer_type", "exact_number")
            if show_mc and options:
                answer_type = "multiple_choice"
            elif answer_type == "multiple_choice" and not options:
                answer_type = "exact_number"

            tasks_public.append(ExamTaskPublic(
                task_id=t.get("task_id", ""),
                skill_id=t.get("skill_id", ""),
                skill_name=t.get("skill_name", ""),
                difficulty=t.get("difficulty", "B"),
                text=t.get("question_text", t.get("text", "")),
                question_latex=t.get("question_latex"),
                options=options,
                options_latex=_build_mc_options_latex(t, options) if options else None,
                image_url=t.get("question_image_url"),
                answer_type=answer_type,
                points=10,
            ))

        return {
            "status": "in_progress",
            "variant_number": session.variant_number,
            "time_minutes": exam.total_time_minutes,
            "time_remaining_seconds": remaining_sec,
            "saved_answers": session.answers or {},
            "tasks": [t.model_dump() for t in tasks_public],
        }

    return {
        "status": session.status,
        "exam_status": exam.status,
    }


def _lock_attempt(db: Session, session_id: str) -> Optional[ExamSession]:
    """The attempt's own row, locked FOR UPDATE and re-read: one student's writes serialize with each other (a double
    click, a retry that overtakes the first try) and with a finalize - never with the other students of the class."""
    return (
        db.query(ExamSession)
        .filter(ExamSession.id == session_id)
        .with_for_update()
        .populate_existing()
        .first()
    )


def _exam_after_attempt_lock(db: Session, exam_id: str) -> Optional[GeneratedExam]:
    """The exam, read PLAIN (no lock) and fresh, after the attempt row is locked.

    Every writer that closes an exam locks exam -> sessions and holds all session locks until it commits, so an
    attempt lock obtained here means either «the exam was closed before» (this read sees it) or «the finalize is
    still waiting for this very lock and will grade what this transaction saves».  The shared exam row is never
    locked by a save or a submit: 60 students writing at once do not queue on one row.
    """
    return (
        db.query(GeneratedExam)
        .filter(GeneratedExam.id == exam_id)
        .populate_existing()
        .first()
    )


@router.post("/exam-sessions/{session_id}/progress",
             summary="Промежуточное автосохранение ответов (студент)")
def save_exam_progress(
    session_id: str,
    req: SaveProgressRequest,
    db: Session = Depends(get_db),
    session_token: Optional[str] = Header(None, alias="X-Exam-Session-Token"),
):
    # A plain ``def``: FastAPI runs it in the thread pool, so a wait for a row lock blocks one thread (bounded by the
    # server's lock_timeout), never the event loop that every other student of the worker is served by.
    session = _lock_attempt(db, session_id)
    if not session:
        raise HTTPException(404, "Сессия не найдена")
    _require_session_capability(session, session_token)
    exam = _exam_after_attempt_lock(db, session.exam_id)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    if _exam_deadline_passed(db, exam):
        db.commit()  # let go of the attempt first: the finalizer locks exam -> sessions
        _check_and_auto_close_exam(db, exam)
        raise HTTPException(410, "Экзамен уже завершён; ответы больше не изменяются")
    if exam.status != "active":
        raise HTTPException(410, "Экзамен уже завершён; ответы больше не изменяются")
    if session.status != "in_progress":
        return {"ok": False, "status": session.status, "message": "Сессия не активна"}

    _validate_answer_keys(exam, session, req.answers)

    # Merge answers into session
    curr_answers = dict(session.answers or {})
    if req.answers:
        curr_answers.update(req.answers)
    session.answers = curr_answers

    started_at = _exam_started_at(db, exam) or _to_naive_utc(session.started_at)
    if started_at:
        total_seconds = (exam.total_time_minutes or 45) * 60
        session.time_spent_seconds = min(
            total_seconds,
            max(0, int((datetime.utcnow() - started_at).total_seconds())),
        )

    db.commit()
    return {"ok": True, "answered_count": _count_answered(curr_answers)}


def _stored_submit_result(session: ExamSession) -> SubmitExamResponse:
    """The body of the submit that graded this attempt, rebuilt from what that submit stored."""
    max_score = session.max_score or 0
    score = session.score or 0
    return SubmitExamResponse(
        session_id=session.id,
        score=score,
        max_score=max_score,
        answered_count=_count_answered(session.answers),
        correct_count=session.correct_count or 0,
        total_tasks=session.total_tasks or 0,
        percentage=round(score / max_score * 100, 1) if max_score else 0.0,
    )


@router.post("/exam-sessions/{session_id}/submit", response_model=SubmitExamResponse,
             summary="Сдать экзамен (студент)")
def submit_exam(
    session_id: str,
    req: SubmitExamRequest,
    db: Session = Depends(get_db),
    session_token: Optional[str] = Header(None, alias="X-Exam-Session-Token"),
):
    session = _lock_attempt(db, session_id)
    if not session:
        raise HTTPException(404, "Сессия не найдена")
    _require_session_capability(session, session_token)
    if session.status == "submitted":
        # A retry that overtook (or followed) a submit that went through - a double click, a response lost on the way,
        # a timer that graded the attempt a moment before: the attempt was graded ONCE; answer with that result, the
        # same body the first submit returned.  The late request's answers are not taken - the attempt is final.
        result = _stored_submit_result(session)
        db.commit()
        return result
    exam = _exam_after_attempt_lock(db, session.exam_id)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    if _exam_deadline_passed(db, exam):
        db.commit()  # let go of the attempt first: the finalizer locks exam -> sessions
        _check_and_auto_close_exam(db, exam)
        raise HTTPException(410, "Экзамен уже завершён; сохранённые ответы отправлены на проверку")
    if exam.status != "active":
        raise HTTPException(410, "Экзамен уже завершён; сохранённые ответы отправлены на проверку")
    if exam.status == "lobby" or session.status == "waiting":
        raise HTTPException(400, "Экзамен ещё не начат преподавателем")

    _validate_answer_keys(exam, session, req.answers)
    started_at = _exam_started_at(db, exam) or _to_naive_utc(session.started_at)
    total_seconds = (exam.total_time_minutes or 45) * 60
    server_time_spent = (
        min(total_seconds, max(0, int((datetime.utcnow() - started_at).total_seconds())))
        if started_at else 0
    )

    # Answers autosaved by ``progress`` that the request body does not repeat (a second tab/device, a stale client)
    # are kept; the body wins where it speaks, exactly as ``progress`` merges.
    merged_answers = {**(session.answers or {}), **(req.answers or {})}
    res = _grade_and_submit_session(
        db, session, exam, merged_answers, server_time_spent
    )

    return SubmitExamResponse(
        session_id=session_id,
        score=res["score"],
        max_score=res["max_score"],
        answered_count=_count_answered(merged_answers),
        correct_count=res["correct_count"],
        total_tasks=res["total_tasks"],
        percentage=res["percentage"],
    )


@router.get("/exam-sessions/{session_id}/results", response_model=SessionResultResponse,
            summary="Результаты экзамена")
def get_results(
    session_id: str,
    db: Session = Depends(get_db),
    session_token: Optional[str] = Header(None, alias="X-Exam-Session-Token"),
):
    session = SessionRepository(db).get_by_id(session_id)
    if not session:
        raise HTTPException(404, "Сессия не найдена")
    _require_session_capability(session, session_token)
    if session.status != "submitted":
        raise HTTPException(400, "Экзамен ещё не сдан")

    exam = ExamRepository(db).get_by_id(session.exam_id)
    # The answer key may be shown only when the exam is over for everyone:
    # closed by the teacher or by the common timer (which finalizes all
    # sessions) or archived. A single submitted session is not enough.
    key_visible = bool(
        exam and (_check_and_auto_close_exam(db, exam) or exam.status in {"closed", "archived"})
    )
    tasks_raw = session_tasks(exam, session)

    # The verdicts shown and the score shown come from one grading: the one stored at submit, or - for a session
    # submitted before verdicts were stored - one regrade that also rewrites its score (see grading.regrade_if_stale).
    if regrade_if_stale(session, exam):
        db.commit()
        db.refresh(session)
    verdicts = stored_verdicts(session.distractor_analytics) or {}
    dist_analytics = session.distractor_analytics or {}
    results_mc_mode = (exam.settings or {}).get("mc_mode", "auto") if exam else "auto"
    task_items = []
    for t in tasks_raw:
        task_id = t.get("task_id", "")
        correct_answer = t.get("correct_answer", "")
        student_answer = (session.answers or {}).get(task_id)
        options = (
            _build_session_mc_options(exam, session, t)
            if exam and _should_show_mc(t, (exam.settings or {}).get("mc_mode", "auto"))
            else []
        )
        if not key_visible:
            task_items.append(TaskResultItem(
                task_id=task_id,
                text=t.get("question_text", ""),
                question_latex=t.get("question_latex"),
                student_answer=student_answer,
                options=options,
                skill_name=t.get("skill_name", ""),
            ))
            continue
        is_correct = verdicts[task_id] if task_id in verdicts else check_task_answer(t, student_answer or "", results_mc_mode)
        d_hit = dist_analytics.get(task_id)
        hit_explanation = ""
        if d_hit:
            hit_explanation = resolve_analytics_explanation(
                d_hit,
                student_answer=student_answer or "",
                distractor_meta=t.get("distractor_meta"),
                match_fn=_match_distractor,
            )
        task_items.append(TaskResultItem(
            task_id=task_id,
            text=t.get("question_text", ""),
            question_latex=t.get("question_latex"),
            student_answer=student_answer,
            correct_answer=correct_answer,
            correct_answer_latex=t.get("correct_answer_latex"),
            is_correct=is_correct,
            options=options,
            skill_name=t.get("skill_name", ""),
            distractor_hit=DistractorHit(
                distractor_value=d_hit["distractor_value"],
                error_type=d_hit["error_type"],
                explanation=hit_explanation,
            ) if d_hit else None,
        ))

    max_score = session.max_score or (len(tasks_raw) * 10)
    return SessionResultResponse(
        session_id=session_id,
        exam_title=exam.title if exam else "",
        student_name=session.student_name,
        variant_number=session.variant_number,
        score=session.score,
        max_score=max_score,
        answered_count=_count_answered(session.answers),
        correct_count=session.correct_count,
        total_tasks=session.total_tasks,
        percentage=round(session.score / max_score * 100, 1) if max_score else 0.0,
        time_spent_seconds=session.time_spent_seconds,
        submitted_at=session.submitted_at,
        answer_key_available=key_visible,
        message=None if key_visible else "Разбор заданий будет доступен после завершения экзамена",
        tasks=task_items,
    )


# ═══════════════════════════════════════════════════════════════════════════
# TEXTBOOK catalog endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/textbooks", response_model=List[TextbookInfo],
            summary="Список учебников")
def list_textbooks(
    class_level: Optional[int] = Query(None, ge=5, le=11),
):
    repo = TextbookRepository()
    rows = repo.list_textbooks(class_level)
    return [TextbookInfo(**r) for r in rows]


@router.get("/textbooks/{textbook_id}/toc", response_model=List[TocNode],
            summary="Оглавление учебника")
def get_toc(textbook_id: str):
    rows = TextbookRepository().get_toc(textbook_id)
    if not rows:
        raise HTTPException(404, "Учебник не найден или пуст")
    return [TocNode(**r) for r in rows]


@router.get("/textbooks/{textbook_id}/coverage", response_model=List[CoverageItem],
            summary="Покрытие учебника задачами")
def get_coverage(textbook_id: str):
    rows = TextbookRepository().get_coverage(textbook_id)
    return [CoverageItem(**r) for r in rows]


@router.get("/toc/{toc_id}/skills", response_model=List[SkillInParagraph],
            summary="Навыки параграфа")
def get_toc_skills(toc_id: int):
    rows = TextbookRepository().get_toc_skills(toc_id)
    return [SkillInParagraph(**r) for r in rows]


# ── must be before /{textbook_id} to avoid route shadowing ────────────────

@router.get("/textbooks/skills/tree", response_model=List[SkillTreeNode],
            summary="Дерево навыков (L1→L4) для режима «По программе»")
def get_skill_tree(
    class_level: Optional[int] = Query(None, ge=5, le=11),
):
    rows = TextbookRepository().get_skill_tree(class_level)
    by_id: dict = {}
    for r in rows:
        r_dict = dict(r)
        if not r_dict.get("name"):
            r_dict["name"] = r_dict.get("name_ru") or r_dict.get("id")
        if not r_dict.get("name_ru"):
            r_dict["name_ru"] = r_dict.get("name") or r_dict.get("id")
        node = SkillTreeNode(**r_dict)
        by_id[node.id] = node

    roots: List[SkillTreeNode] = []
    for node in by_id.values():
        if node.parent_id and node.parent_id in by_id:
            by_id[node.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


@router.get("/textbooks/{textbook_id}", response_model=TextbookDetail,
            summary="Полная информация об учебнике")
def get_textbook(textbook_id: str):
    row = TextbookRepository().get_by_id(textbook_id)
    if not row:
        raise HTTPException(404, "Учебник не найден")
    return TextbookDetail(**row)


@router.get("/textbooks/{textbook_id}/toc/tree", response_model=List[TocNode],
            summary="Оглавление учебника (вложенное дерево)")
def get_toc_tree(textbook_id: str):
    rows = TextbookRepository().get_toc(textbook_id)
    if not rows:
        raise HTTPException(404, "Учебник не найден или пуст")
    flat = [TocNode(**r) for r in rows]
    by_id = {n.id: n for n in flat}
    roots: List[TocNode] = []
    for node in flat:
        if node.parent_id and node.parent_id in by_id:
            by_id[node.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


@router.get("/textbooks/{textbook_id}/tasks", response_model=List[TextbookTaskInfo],
            summary="Задачи учебника с фильтрацией")
def get_textbook_tasks(
    textbook_id: str,
    toc_id: Optional[int] = Query(None, description="Фильтр по разделу TOC"),
    skill_id: Optional[str] = Query(None),
    difficulty: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    rows = TextbookRepository().get_tasks(
        textbook_id,
        toc_id=toc_id,
        skill_id=skill_id,
        difficulty=difficulty,
        limit=limit,
        offset=offset,
    )
    return [TextbookTaskInfo(**r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# TEACHER — additional management endpoints
# ═══════════════════════════════════════════════════════════════════════════


class SessionSummary(BaseModel):
    session_id: str
    student_name: str
    variant_number: int
    status: str
    score: int
    max_score: int
    correct_count: int
    total_tasks: int
    percentage: float
    time_spent_seconds: int
    started_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None


class StudentExamHistoryItem(BaseModel):
    """A completed student attempt without answers or answer-key material."""

    id: str
    exam_id: str
    exam_title: str
    score: int
    max_score: int
    correct_count: int
    total_tasks: int
    time_spent_seconds: int
    status: Literal["submitted"]
    started_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None


@router.get(
    "/students/{student_id}/exam-history",
    response_model=List[StudentExamHistoryItem],
    summary="История завершённых экзаменов ученика",
)
def list_student_exam_history(
    student_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """Return only completed attempts visible to the current staff member.

    The profile should never imply that a lobby/waiting attempt is a result.
    Printed exams are intentionally absent until a future marking workflow
    stores per-student results in ``exam_sessions``.
    """
    _await(require_student_history_access(actor, student_id), db)
    rows = (
        db.query(ExamSession, GeneratedExam)
        .join(GeneratedExam, GeneratedExam.id == ExamSession.exam_id)
        .filter(
            ExamSession.student_id == str(student_id),
            ExamSession.status == "submitted",
            GeneratedExam.org_id == actor.org_id,
        )
        .order_by(ExamSession.submitted_at.desc(), ExamSession.id.desc())
        .limit(100)
        .all()
    )
    return [
        StudentExamHistoryItem(
            id=session.id,
            exam_id=exam.id,
            exam_title=exam.title,
            score=session.score or 0,
            max_score=session.max_score or 0,
            correct_count=session.correct_count or 0,
            total_tasks=session.total_tasks or 0,
            time_spent_seconds=session.time_spent_seconds or 0,
            status="submitted",
            started_at=session.started_at,
            submitted_at=session.submitted_at,
        )
        for session, exam in rows
    ]


@router.get("/exams/{exam_id}/sessions", response_model=List[SessionSummary],
            summary="Список сессий экзамена (учитель)")
def list_exam_sessions(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    _get_exam_for_actor(db, exam_id, actor, "sessions")
    sessions = (
        db.query(ExamSession)
        .filter(ExamSession.exam_id == exam_id)
        .order_by(ExamSession.started_at.desc())
        .all()
    )
    result = []
    for s in sessions:
        ms = s.max_score or 1
        result.append(SessionSummary(
            session_id=s.id,
            student_name=s.student_name,
            variant_number=s.variant_number,
            status=s.status,
            score=s.score,
            max_score=s.max_score,
            correct_count=s.correct_count,
            total_tasks=s.total_tasks,
            percentage=round(s.score / ms * 100, 1) if ms else 0.0,
            time_spent_seconds=s.time_spent_seconds,
            started_at=s.started_at,
            submitted_at=s.submitted_at,
        ))
    return result


@router.post("/exams/{exam_id}/close", summary="Закрыть экзамен")
def close_exam(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    _get_exam_for_teacher_operator(db, exam_id, actor, "close")
    exam = (
        db.query(GeneratedExam)
        .filter(GeneratedExam.id == exam_id)
        .with_for_update()
        .populate_existing()  # re-read under the lock: a timer finalize may have closed it a moment ago
        .first()
    )
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    finalized_count = _finalize_exam(db, exam)
    return {
        "exam_id": exam_id,
        "status": "closed",
        "finalized_sessions": finalized_count,
    }


@router.post("/exams/{exam_id}/generate-variants",
             summary="Запустить генерацию вариантов для экзамена")
def generate_variants(
    exam_id: str,
    variants_count: int = Query(5, ge=2, le=30),
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    exam = _get_exam_for_teacher_operator(db, exam_id, actor)
    if exam.status not in ("draft", "variants_ready"):
        raise HTTPException(400, "Генерация вариантов возможна только для черновиков")

    tasks_raw = (exam.variants or [{}])[0].get("tasks", [])
    if not tasks_raw:
        raise HTTPException(400, "Экзамен не содержит задач")

    try:
        orch = VariantOrchestrator(db=db)
        report = orch.generate_variants(tasks_raw, variants_count, seed=42)
        ExamRepository(db).update_variants(
            exam,
            [v.to_dict() for v in report.variants],
            report.total_variants,
        )
        return {
            "exam_id": exam_id,
            "status": "variants_ready",
            "variants_count": report.total_variants,
            "templatized_tasks": report.tasks_templatized,
            "original_tasks": report.tasks_fallback,
        }
    except Exception as exc:
        log.error("Variant generation failed for %s: %s", exam_id, exc)
        raise HTTPException(500, f"Ошибка генерации вариантов: {exc}")


class ExamAnalyticsResponse(BaseModel):
    exam_id: str
    title: str
    total_sessions: int
    submitted_sessions: int
    avg_score_pct: float
    avg_time_seconds: float
    pass_rate_pct: float
    # Единый список задач: correct_rate + distractor_hits вместе
    # Отсортировано от худшего к лучшему
    task_analytics: List[dict] = []
    # Топ-5 самых частых ошибок по всему экзамену
    top_errors: List[dict] = []


@router.get("/exams/{exam_id}/analytics", response_model=ExamAnalyticsResponse,
            summary="Аналитика экзамена (учитель)")
def get_exam_analytics(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    exam = _get_exam_for_actor(db, exam_id, actor)

    sessions = (
        db.query(ExamSession)
        .filter(ExamSession.exam_id == exam_id)
        .all()
    )
    total = len(sessions)
    submitted = [s for s in sessions if s.status == "submitted"]
    n_submitted = len(submitted)
    # Scores and per-task correctness below must come from one grading (see grading.regrade_if_stale).
    if any([regrade_if_stale(s, exam) for s in submitted]):
        db.commit()

    if n_submitted == 0:
        return ExamAnalyticsResponse(
            exam_id=exam_id,
            title=exam.title,
            total_sessions=total,
            submitted_sessions=0,
            avg_score_pct=0.0,
            avg_time_seconds=0.0,
            pass_rate_pct=0.0,
        )

    avg_pct = sum(
        (s.score / s.max_score * 100) if s.max_score else 0
        for s in submitted
    ) / n_submitted

    avg_time = sum(s.time_spent_seconds for s in submitted) / n_submitted

    pass_rate = sum(
        1 for s in submitted
        if s.max_score and (s.score / s.max_score) >= 0.5
    ) / n_submitted * 100

    # Per-task correctness analysis
    task_correct: Dict[str, int] = {}
    task_total: Dict[str, int] = {}

    # Build task metadata lookup from exam variants (первый вариант = эталон)
    task_lookup: Dict[str, dict] = {}
    for v in (exam.variants or []):
        for t in v.get("tasks", []):
            if t["task_id"] not in task_lookup:
                task_lookup[t["task_id"]] = t

    # {task_id: {explanation_key: {explanation, error_type, count}}}
    task_distractor_hits: Dict[str, Dict[str, dict]] = {}
    mc_mode = (exam.settings or {}).get("mc_mode", "auto")

    for s in submitted:
        verdicts = stored_verdicts(s.distractor_analytics) or {}
        for task_id, student_ans in (s.answers or {}).items():
            task = task_lookup.get(task_id, {})
            task_total[task_id] = task_total.get(task_id, 0) + 1
            if verdicts[task_id] if task_id in verdicts else check_task_answer(task, student_ans, mc_mode):
                task_correct[task_id] = task_correct.get(task_id, 0) + 1

        # Агрегируем дистрактор-попадания
        for task_id, hit in (s.distractor_analytics or {}).items():
            if not isinstance(hit, dict) or not hit.get("hit"):
                continue
            error_type = hit.get("error_type", "unknown")
            task_meta = task_lookup.get(task_id, {})
            explanation = resolve_analytics_explanation(
                hit,
                student_answer=(s.answers or {}).get(task_id, ""),
                distractor_meta=task_meta.get("distractor_meta"),
                match_fn=_match_distractor,
            )
            if not explanation:
                continue
            # Ключ группировки: error_type (+ первые 60 символов для уникальности)
            key = f"{error_type}::{explanation[:60]}"
            bucket = task_distractor_hits.setdefault(task_id, {})
            if key not in bucket:
                bucket[key] = {
                    "explanation": explanation,
                    "error_type": error_type,
                    "count": 0,
                }
            bucket[key]["count"] += 1

    # ── Объединённый список задач ─────────────────────────────────────────────
    all_task_ids = set(task_total.keys()) | set(task_distractor_hits.keys())
    task_analytics = []
    for task_id in all_task_ids:
        total_attempts = task_total.get(task_id, 0)
        correct = task_correct.get(task_id, 0)
        wrong = total_attempts - correct
        rate = round(correct / total_attempts, 3) if total_attempts else 0.0

        task_meta = task_lookup.get(task_id, {})
        # Название темы: skill_name → fallback к toc/question начало
        skill_name = (
            task_meta.get("skill_name")
            or task_meta.get("toc_title")
            or ""
        )
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
            "task_id": task_id,
            **question_display_fields(task_meta),
            "skill_name": skill_name,
            "attempts": total_attempts,
            "correct_count": correct,
            "wrong_count": wrong,
            "correct_rate": rate,
            "distractor_hits": distractor_hits,
        })

    # Сортировка: самые провальные задачи первыми
    task_analytics.sort(key=lambda x: (x["correct_rate"], -x["attempts"]))

    # ── Топ-5 типичных ошибок по всему экзамену ───────────────────────────────
    # Агрегируем одинаковые объяснения из разных задач
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
            error_aggregate[key]["affected_tasks"].append(
                item.get("skill_name") or item["task_id"]
            )

    top_errors = sorted(
        [
            {
                "explanation": v["explanation"],
                "total_count": v["total_count"],
                "affected_skills": list(dict.fromkeys(v["affected_tasks"]))[:3],
                "students_pct": round(v["total_count"] / max(n_submitted, 1) * 100, 1),
            }
            for v in error_aggregate.values()
        ],
        key=lambda x: x["total_count"],
        reverse=True,
    )[:5]

    return ExamAnalyticsResponse(
        exam_id=exam_id,
        title=exam.title,
        total_sessions=total,
        submitted_sessions=n_submitted,
        avg_score_pct=round(avg_pct, 1),
        avg_time_seconds=round(avg_time, 1),
        pass_rate_pct=round(pass_rate, 1),
        task_analytics=task_analytics,
        top_errors=top_errors,
    )


# ═══════════════════════════════════════════════════════════════════════════
# On-demand Exam Report (LLM Narrative)
# ═══════════════════════════════════════════════════════════════════════════


class ExamReportResponse(BaseModel):
    exam_id: str
    title: str
    total_students: int
    submitted_count: int
    avg_score_pct: float
    pass_rate_pct: float
    task_analytics: List[dict] = []
    top_errors: List[dict] = []
    narrative_ru: Optional[str] = None
    narrative_generated: bool = False
    is_final: bool = False
    generated_at: Optional[str] = None


@router.post(
    "/exams/{exam_id}/report",
    response_model=ExamReportResponse,
    summary="Сгенерировать / обновить LLM-отчёт по экзамену (учитель)",
)
def generate_exam_report_endpoint(
    exam_id: str,
    force: bool = Query(False, description="Перегенерировать даже если отчёт уже есть"),
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_heavy_staff),
):
    """
    Генерирует нарратив с помощью Gemini на основе агрегированной аналитики.
    Идемпотентен: повторный вызов вернёт кешированный отчёт (если force=false).
    Параметр ?force=true перезапишет существующий отчёт.
    """
    from src.domain.report_generator import generate_exam_report

    exam = _get_exam_for_teacher_operator(db, exam_id, actor)

    sessions = (
        db.query(ExamSession).filter(ExamSession.exam_id == exam_id).all()
    )

    report = generate_exam_report(db, exam=exam, sessions=sessions, force=force)
    if report is None:
        raise HTTPException(422, "Нет сданных работ — отчёт не может быть сформирован")

    task_analytics, narrative_ru = report_display_projection(
        exam.variants or [], report.task_analytics or [], report.narrative_ru,
    )
    return ExamReportResponse(
        exam_id=exam_id,
        title=exam.title,
        total_students=report.total_students,
        submitted_count=report.submitted_count,
        avg_score_pct=float(report.avg_score_pct or 0),
        pass_rate_pct=float(report.pass_rate_pct or 0),
        task_analytics=task_analytics,
        top_errors=report.top_errors or [],
        narrative_ru=narrative_ru,
        narrative_generated=report.narrative_generated or False,
        is_final=report.is_final or False,
        generated_at=report.generated_at.isoformat() if report.generated_at else None,
    )


@router.get(
    "/exams/{exam_id}/report",
    response_model=ExamReportResponse,
    summary="Получить сохранённый LLM-отчёт по экзамену (учитель)",
)
def get_exam_report_endpoint(
    exam_id: str,
    db: Session = Depends(get_db),
    actor: ExamActor = Depends(require_exam_staff),
):
    """
    Возвращает ранее сгенерированный отчёт.
    Если отчёта ещё нет — 404, учитель должен сначала вызвать POST /exams/{id}/report.
    """
    exam = _get_exam_for_actor(db, exam_id, actor, "report")

    report = ExamReportRepository(db).get_by_exam(exam_id)
    if not report:
        raise HTTPException(
            404,
            "Отчёт ещё не сгенерирован. Вызовите POST /exams/{exam_id}/report",
        )

    task_analytics, narrative_ru = report_display_projection(
        exam.variants or [], report.task_analytics or [], report.narrative_ru,
    )
    return ExamReportResponse(
        exam_id=exam_id,
        title=exam.title,
        total_students=report.total_students,
        submitted_count=report.submitted_count,
        avg_score_pct=float(report.avg_score_pct or 0),
        pass_rate_pct=float(report.pass_rate_pct or 0),
        task_analytics=task_analytics,
        top_errors=report.top_errors or [],
        narrative_ru=narrative_ru,
        narrative_generated=report.narrative_generated or False,
        is_final=report.is_final or False,
        generated_at=report.generated_at.isoformat() if report.generated_at else None,
    )
