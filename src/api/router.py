"""
Exam Service — unified API router.
Merges exam_router.py + textbook_exam.py from monolith into one module.

Endpoints:
  TEACHER
    POST /exams/generate            — create draft exam (topics or textbook mode)
    POST /exams/{id}/publish        — publish → access_code
    GET  /exams/{id}                — exam detail (teacher view)
    GET  /exams                     — list teacher's exams
  STUDENT (public, by access_code)
    GET  /exams/by-code/{code}      — exam info
    POST /exams/by-code/{code}/start — start session
    POST /exam-sessions/{id}/submit  — submit answers
    GET  /exam-sessions/{id}/results — results
  TEXTBOOK (catalog)
    GET  /textbooks                 — list textbooks
    GET  /textbooks/{id}/toc        — table of contents
    GET  /textbooks/{id}/coverage   — digitization coverage
    GET  /toc/{id}/skills           — skills for a paragraph
"""
from __future__ import annotations

import logging
import random
import string
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.core.exceptions import (
    ExamNotFoundError,
    ExamNotActiveError,
    SessionNotFoundError,
    SessionAlreadySubmittedError,
    NoTasksAvailableError,
)
from src.models import GeneratedExam, ExamSession
from src.repositories.task_repo import TaskRepository, TextbookRepository
from src.repositories.exam_repo import ExamRepository, SessionRepository

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
    # Common
    tasks_count: int = Field(10, ge=1, le=50)
    difficulty_distribution: Optional[Dict[str, float]] = None  # easy/medium/hard → %
    time_minutes: int = Field(45, ge=5, le=180)
    variants_count: int = Field(1, ge=1, le=30)
    generate_variants: bool = False
    shuffle_tasks: bool = True
    mc_mode: str = "auto"


class PublishExamRequest(BaseModel):
    teacher_name: Optional[str] = None
    subject: Optional[str] = None


class ExamSummaryResponse(BaseModel):
    exam_id: str
    title: str
    status: str
    access_code: Optional[str] = None
    tasks_per_variant: int
    variants_count: int
    created_at: datetime


class ExamTaskPublic(BaseModel):
    task_id: str
    skill_id: str = ""
    skill_name: str = ""
    difficulty: str = "B"
    text: str
    options: List[str] = []
    image_url: Optional[str] = None
    answer_type: str = "exact_number"
    points: int = 10


class ExamInfoResponse(BaseModel):
    exam_id: str
    title: str
    subject: str
    teacher_name: Optional[str] = None
    total_time_minutes: int
    tasks_per_variant: int
    variants_count: int
    status: str


class StartExamRequest(BaseModel):
    student_name: str = Field(..., min_length=1)
    class_level: Optional[int] = None


class StartExamResponse(BaseModel):
    session_id: str
    variant_number: int
    time_minutes: int
    tasks: List[ExamTaskPublic]


class SubmitExamRequest(BaseModel):
    answers: Dict[str, str]       # {task_id: answer_string}
    time_spent_seconds: int = 0


class SubmitExamResponse(BaseModel):
    session_id: str
    score: int
    max_score: int
    answered_count: int
    correct_count: int
    total_tasks: int
    percentage: float


class TaskResultItem(BaseModel):
    task_id: str
    text: str
    student_answer: Optional[str]
    correct_answer: str
    is_correct: bool
    options: List[str] = []
    skill_name: str = ""


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
    level: str
    number: Optional[str] = None
    title: str
    page_start: Optional[int] = None
    sort_order: int = 0
    tasks_count: int = 0
    children: List["TocNode"] = []


class SkillTreeNode(BaseModel):
    id: str
    level: str
    name: str
    name_ru: str
    parent_id: Optional[str] = None
    class_level_start: Optional[int] = None
    class_level_end: Optional[int] = None
    importance: int = 5
    tasks_count: int = 0
    children: List["SkillTreeNode"] = []


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
    level: str
    number: Optional[str] = None
    title: str
    skills_mapped: int
    tasks_available: int
    is_digitized: bool


class PublishExamResponse(BaseModel):
    exam_id: str
    title: str
    status: str
    access_code: str
    tasks_per_variant: int
    variants_count: int
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
    total_time_minutes: int
    variants_count: int
    tasks_per_variant: int
    settings: Dict[str, Any]
    skills_covered: List[str]
    created_at: datetime
    variants: List[DraftVariant]


class ReplaceTaskRequest(BaseModel):
    variant_number: int = 1
    task_id: str


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _generate_access_code() -> str:
    chars = string.ascii_uppercase + string.digits
    return "EX" + "".join(random.choices(chars, k=8))


def _difficulty_counts(tasks_count: int, dist: Dict[str, float]) -> Dict[str, int]:
    """Convert difficulty_distribution percentages → absolute counts per A/B/C."""
    total_pct = (dist.get("easy", 0) + dist.get("medium", 0) + dist.get("hard", 0)) or 100
    count_a = round(tasks_count * dist.get("easy", 30) / total_pct)
    count_b = round(tasks_count * dist.get("medium", 50) / total_pct)
    count_c = max(0, tasks_count - count_a - count_b)
    return {"A": count_a, "B": count_b, "C": count_c}


def _should_show_mc(task: dict, mc_mode: str = "auto") -> bool:
    if mc_mode == "all_open":
        return False
    has_options = bool(task.get("answer_options") or task.get("distractor_meta"))
    if mc_mode == "all_mc":
        return has_options
    answer_type = task.get("answer_type", "exact_number")
    difficulty = (task.get("difficulty") or "B").upper()
    mc_types = {"exact_number", "decimal", "multiple_choice", "true_false", "fraction"}
    mc_diffs = {"A", "B"}
    return answer_type in mc_types and difficulty in mc_diffs and has_options


def _build_mc_options(task: dict) -> List[str]:
    correct = task.get("correct_answer", "")
    options: list = task.get("answer_options") or []
    if options:
        if correct and correct not in options:
            options = [correct] + list(options)
        options = list(options)
        random.shuffle(options)
        return options
    distractors = task.get("distractor_meta") or []
    if distractors and correct:
        opts = [correct] + [str(d.get("value", "")) for d in distractors[:3]]
        random.shuffle(opts)
        return opts
    return []


def _normalise_answer(answer: str) -> str:
    return answer.strip().lower().replace(",", ".") if answer else ""


def _check_answer(student: str, correct: str) -> bool:
    s, c = _normalise_answer(student), _normalise_answer(correct)
    if s == c:
        return True
    try:
        return abs(float(s) - float(c)) < 0.01
    except (ValueError, TypeError):
        return False


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
            used.extend(r["task_id"] for r in rows)
        # Fill remainder if not enough per difficulty
        if len(all_tasks) < req.tasks_count:
            fill = task_repo.get_tasks_by_skills(
                req.skill_ids, None,
                req.tasks_count - len(all_tasks),
                [t["task_id"] for t in all_tasks],
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
            used.extend(r["task_id"] for r in rows)
        if len(all_tasks) < req.tasks_count:
            fill = task_repo.get_tasks_by_toc_ids(
                toc_ids_expanded, None,
                req.tasks_count - len(all_tasks),
                [t["task_id"] for t in all_tasks],
            )
            all_tasks.extend(fill)

    return all_tasks[: req.tasks_count]


# ═══════════════════════════════════════════════════════════════════════════
# TEACHER endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/exams/generate", summary="Создать черновик экзамена")
async def generate_exam(req: GenerateExamRequest, db: Session = Depends(get_db)):
    if not req.skill_ids and not req.paragraph_ids:
        raise HTTPException(400, "Укажите skill_ids или paragraph_ids")

    task_repo = TaskRepository()
    exam_repo = ExamRepository(db)

    tasks = _build_task_pool(req, task_repo)
    if not tasks:
        raise HTTPException(
            400,
            "Не найдено задач. Проверьте, что задачи оцифрованы и активны.",
        )

    if req.shuffle_tasks:
        random.shuffle(tasks)

    source = "topics" if req.skill_ids else "textbook"
    exam_id = f"EXAM_{uuid4().hex[:8]}"
    skills_set = {t["skill_id"] for t in tasks if t["skill_id"]}

    exam = GeneratedExam(
        id=exam_id,
        title=req.title,
        source_mode=source,
        variants=[{"variant_number": 1, "tasks": tasks}],
        settings={
            "time_minutes": req.time_minutes,
            "shuffle_tasks": req.shuffle_tasks,
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

    exam_repo.create(exam)
    log.info("Exam created: %s, %d tasks, source=%s", exam_id, len(tasks), source)

    return {
        "exam_id": exam_id,
        "status": exam.status,
        "tasks_count": len(tasks),
        "variants_count": exam.variants_count,
        "skills_covered": list(skills_set),
    }


@router.get("/exams", response_model=List[ExamSummaryResponse],
            summary="Список экзаменов")
async def list_exams(db: Session = Depends(get_db)):
    exams = (
        db.query(GeneratedExam)
        .order_by(GeneratedExam.created_at.desc())
        .limit(100)
        .all()
    )
    return [
        ExamSummaryResponse(
            exam_id=e.id,
            title=e.title,
            status=e.status,
            access_code=e.access_code,
            tasks_per_variant=e.tasks_per_variant,
            variants_count=e.variants_count,
            created_at=e.created_at,
        )
        for e in exams
    ]


@router.post("/exams/{exam_id}/publish", response_model=PublishExamResponse,
             summary="Опубликовать экзамен → получить access_code")
async def publish_exam(
    exam_id: str,
    req: PublishExamRequest,
    db: Session = Depends(get_db),
):
    exam_repo = ExamRepository(db)
    exam = exam_repo.get_by_id(exam_id)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    if exam.status == "active":
        raise HTTPException(400, "Экзамен уже опубликован")

    code = _generate_access_code()
    if req.teacher_name:
        exam.teacher_name = req.teacher_name
    if req.subject:
        exam.subject = req.subject
    exam_repo.publish(exam, code)

    return PublishExamResponse(
        exam_id=exam.id,
        title=exam.title,
        status=exam.status,
        access_code=exam.access_code,
        tasks_per_variant=exam.tasks_per_variant,
        variants_count=exam.variants_count,
        created_at=exam.created_at,
        url=f"/e/{exam.access_code}",
    )


@router.get("/exams/{exam_id}/draft", response_model=DraftExamResponse,
            summary="Черновик экзамена с задачами (учитель)")
async def get_exam_draft(exam_id: str, db: Session = Depends(get_db)):
    exam = ExamRepository(db).get_by_id(exam_id)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
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
                answer_options=t.get("answer_options"),
                distractor_meta=t.get("distractor_meta"),
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
        total_time_minutes=exam.total_time_minutes,
        variants_count=exam.variants_count,
        tasks_per_variant=exam.tasks_per_variant,
        settings=exam.settings or {},
        skills_covered=exam.skills_covered or [],
        created_at=exam.created_at,
        variants=variants_out,
    )


@router.get("/exams/{exam_id}/draft/alternatives",
            summary="Альтернативные задачи для замены (учитель)")
async def get_task_alternatives(
    exam_id: str,
    variant_number: int = Query(1),
    task_id: str = Query(...),
    db: Session = Depends(get_db),
):
    exam = ExamRepository(db).get_by_id(exam_id)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
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
async def replace_task(
    exam_id: str,
    req: ReplaceTaskRequest,
    db: Session = Depends(get_db),
):
    import copy
    exam = ExamRepository(db).get_by_id(exam_id)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    if exam.status not in ("draft", "variants_ready"):
        raise HTTPException(400, "Замена задач возможна только в черновике")

    new_variants = copy.deepcopy(exam.variants or [])
    variant = next(
        (v for v in new_variants if v.get("variant_number") == req.variant_number),
        None,
    )
    if not variant:
        raise HTTPException(404, "Вариант не найден")
    old_task = next(
        (t for t in variant.get("tasks", []) if t.get("task_id") == req.task_id),
        None,
    )
    if not old_task:
        raise HTTPException(404, "Задача не найдена")

    used_ids = [t.get("task_id") for t in variant.get("tasks", [])]
    skill_ids = [old_task["skill_id"]] if old_task.get("skill_id") else []
    task_repo = TaskRepository()
    alternatives = (
        task_repo.get_tasks_by_skills(skill_ids, old_task.get("difficulty"), 1, used_ids)
        if skill_ids else []
    )
    if not alternatives:
        alternatives = (
            task_repo.get_tasks_by_skills(skill_ids, None, 1, used_ids)
            if skill_ids else []
        )
    if not alternatives:
        raise HTTPException(400, "Нет доступных задач для замены")

    new_task = alternatives[0]
    variant["tasks"] = [
        new_task if t.get("task_id") == req.task_id else t
        for t in variant.get("tasks", [])
    ]
    exam.variants = new_variants
    db.add(exam)
    db.commit()
    db.refresh(exam)
    return {"status": "replaced", "new_task": new_task}


@router.get("/exams/{exam_id}", response_model=ExamSummaryResponse,
            summary="Информация об экзамене (учитель)")
async def get_exam(exam_id: str, db: Session = Depends(get_db)):
    exam = ExamRepository(db).get_by_id(exam_id)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    return ExamSummaryResponse(
        exam_id=exam.id,
        title=exam.title,
        status=exam.status,
        access_code=exam.access_code,
        tasks_per_variant=exam.tasks_per_variant,
        variants_count=exam.variants_count,
        created_at=exam.created_at,
    )


# ═══════════════════════════════════════════════════════════════════════════
# STUDENT endpoints (public)
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/exams/by-code/{code}", response_model=ExamInfoResponse,
            summary="Информация об экзамене по коду (студент)")
async def get_exam_by_code(code: str, db: Session = Depends(get_db)):
    exam = ExamRepository(db).get_by_access_code(code)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    if exam.status != "active":
        raise HTTPException(410, "Экзамен закрыт или ещё не опубликован")
    return ExamInfoResponse(
        exam_id=exam.id,
        title=exam.title,
        subject=exam.subject or "Математика",
        teacher_name=exam.teacher_name,
        total_time_minutes=exam.total_time_minutes,
        tasks_per_variant=exam.tasks_per_variant,
        variants_count=exam.variants_count,
        status=exam.status,
    )


@router.post("/exams/by-code/{code}/start", response_model=StartExamResponse,
             summary="Начать экзамен (студент)")
async def start_exam(
    code: str,
    req: StartExamRequest,
    db: Session = Depends(get_db),
):
    exam = ExamRepository(db).get_by_access_code(code)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    if exam.status != "active":
        raise HTTPException(410, "Экзамен закрыт")

    variants = exam.variants or []
    if not variants:
        raise HTTPException(500, "Экзамен не содержит вариантов")

    variant = random.choice(variants)
    tasks_raw = variant.get("tasks", [])
    mc_mode = (exam.settings or {}).get("mc_mode", "auto")

    session_id = f"SES_{uuid4().hex[:8]}"
    session = ExamSession(
        id=session_id,
        exam_id=exam.id,
        student_name=req.student_name,
        class_level=req.class_level,
        variant_number=variant.get("variant_number", 1),
        answers={},
        max_score=len(tasks_raw) * 10,
        status="in_progress",
    )
    SessionRepository(db).create(session)

    tasks_public = []
    for t in tasks_raw:
        show_mc = _should_show_mc(t, mc_mode)
        options = _build_mc_options(t) if show_mc else []
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
            options=options,
            image_url=t.get("question_image_url"),
            answer_type=answer_type,
            points=10,
        ))

    return StartExamResponse(
        session_id=session_id,
        variant_number=session.variant_number,
        time_minutes=exam.total_time_minutes,
        tasks=tasks_public,
    )


@router.post("/exam-sessions/{session_id}/submit", response_model=SubmitExamResponse,
             summary="Сдать экзамен (студент)")
async def submit_exam(
    session_id: str,
    req: SubmitExamRequest,
    db: Session = Depends(get_db),
):
    sess_repo = SessionRepository(db)
    session = sess_repo.get_by_id(session_id)
    if not session:
        raise HTTPException(404, "Сессия не найдена")
    if session.status == "submitted":
        raise HTTPException(400, "Экзамен уже сдан")

    exam = ExamRepository(db).get_by_id(session.exam_id)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")

    # Find variant tasks
    variants = exam.variants or []
    variant = next(
        (v for v in variants if v.get("variant_number") == session.variant_number),
        variants[0] if variants else None,
    )
    tasks_raw = variant.get("tasks", []) if variant else []

    # Grade
    correct_count = 0
    for t in tasks_raw:
        task_id = t.get("task_id", "")
        student_ans = req.answers.get(task_id, "")
        if _check_answer(student_ans, t.get("correct_answer", "")):
            correct_count += 1

    total_tasks = len(tasks_raw)
    score = correct_count * 10
    max_score = total_tasks * 10

    sess_repo.submit(
        session, req.answers, score, max_score,
        correct_count, total_tasks, req.time_spent_seconds,
    )

    return SubmitExamResponse(
        session_id=session_id,
        score=score,
        max_score=max_score,
        answered_count=len(req.answers),
        correct_count=correct_count,
        total_tasks=total_tasks,
        percentage=round(score / max_score * 100, 1) if max_score else 0.0,
    )


@router.get("/exam-sessions/{session_id}/results", response_model=SessionResultResponse,
            summary="Результаты экзамена")
async def get_results(session_id: str, db: Session = Depends(get_db)):
    session = SessionRepository(db).get_by_id(session_id)
    if not session:
        raise HTTPException(404, "Сессия не найдена")
    if session.status != "submitted":
        raise HTTPException(400, "Экзамен ещё не сдан")

    exam = ExamRepository(db).get_by_id(session.exam_id)
    variants = (exam.variants or []) if exam else []
    variant = next(
        (v for v in variants if v.get("variant_number") == session.variant_number),
        variants[0] if variants else None,
    )
    tasks_raw = variant.get("tasks", []) if variant else []

    task_items = []
    for t in tasks_raw:
        task_id = t.get("task_id", "")
        correct_answer = t.get("correct_answer", "")
        student_answer = (session.answers or {}).get(task_id)
        is_correct = _check_answer(student_answer or "", correct_answer)
        options = _build_mc_options(t) if t.get("answer_options") else []
        task_items.append(TaskResultItem(
            task_id=task_id,
            text=t.get("question_text", ""),
            student_answer=student_answer,
            correct_answer=correct_answer,
            is_correct=is_correct,
            options=options,
            skill_name=t.get("skill_name", ""),
        ))

    max_score = session.max_score or (len(tasks_raw) * 10)
    return SessionResultResponse(
        session_id=session_id,
        exam_title=exam.title if exam else "",
        student_name=session.student_name,
        variant_number=session.variant_number,
        score=session.score,
        max_score=max_score,
        answered_count=len(session.answers or {}),
        correct_count=session.correct_count,
        total_tasks=session.total_tasks,
        percentage=round(session.score / max_score * 100, 1) if max_score else 0.0,
        time_spent_seconds=session.time_spent_seconds,
        submitted_at=session.submitted_at,
        tasks=task_items,
    )


# ═══════════════════════════════════════════════════════════════════════════
# TEXTBOOK catalog endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/textbooks", response_model=List[TextbookInfo],
            summary="Список учебников")
async def list_textbooks(
    class_level: Optional[int] = Query(None, ge=5, le=11),
):
    repo = TextbookRepository()
    rows = repo.list_textbooks(class_level)
    return [TextbookInfo(**r) for r in rows]


@router.get("/textbooks/{textbook_id}/toc", response_model=List[TocNode],
            summary="Оглавление учебника")
async def get_toc(textbook_id: str):
    rows = TextbookRepository().get_toc(textbook_id)
    if not rows:
        raise HTTPException(404, "Учебник не найден или пуст")
    return [TocNode(**r) for r in rows]


@router.get("/textbooks/{textbook_id}/coverage", response_model=List[CoverageItem],
            summary="Покрытие учебника задачами")
async def get_coverage(textbook_id: str):
    rows = TextbookRepository().get_coverage(textbook_id)
    return [CoverageItem(**r) for r in rows]


@router.get("/toc/{toc_id}/skills", response_model=List[SkillInParagraph],
            summary="Навыки параграфа")
async def get_toc_skills(toc_id: int):
    rows = TextbookRepository().get_toc_skills(toc_id)
    return [SkillInParagraph(**r) for r in rows]


# ── must be before /{textbook_id} to avoid route shadowing ────────────────

@router.get("/textbooks/skills/tree", response_model=List[SkillTreeNode],
            summary="Дерево навыков (L1→L4) для режима «По программе»")
async def get_skill_tree(
    class_level: Optional[int] = Query(None, ge=5, le=11),
):
    rows = TextbookRepository().get_skill_tree(class_level)
    by_id: dict = {r["id"]: SkillTreeNode(**r) for r in rows}
    roots: List[SkillTreeNode] = []
    for node in by_id.values():
        if node.parent_id and node.parent_id in by_id:
            by_id[node.parent_id].children.append(node)
        else:
            roots.append(node)
    if class_level:
        prefix = f"G{class_level}_"
        has_grade = any(r.level == "L1" and r.id.startswith(prefix) for r in roots)
        if has_grade:
            roots = [r for r in roots if r.level != "L1" or r.id.startswith(prefix)]
    return roots


@router.get("/textbooks/{textbook_id}", response_model=TextbookDetail,
            summary="Полная информация об учебнике")
async def get_textbook(textbook_id: str):
    row = TextbookRepository().get_by_id(textbook_id)
    if not row:
        raise HTTPException(404, "Учебник не найден")
    return TextbookDetail(**row)


@router.get("/textbooks/{textbook_id}/toc/tree", response_model=List[TocNode],
            summary="Оглавление учебника (вложенное дерево)")
async def get_toc_tree(textbook_id: str):
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
async def get_textbook_tasks(
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


@router.get("/exams/{exam_id}/sessions", response_model=List[SessionSummary],
            summary="Список сессий экзамена (учитель)")
async def list_exam_sessions(exam_id: str, db: Session = Depends(get_db)):
    exam = ExamRepository(db).get_by_id(exam_id)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
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
async def close_exam(exam_id: str, db: Session = Depends(get_db)):
    exam = ExamRepository(db).get_by_id(exam_id)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
    ExamRepository(db).close(exam)
    return {"exam_id": exam_id, "status": "closed"}


@router.post("/exams/{exam_id}/generate-variants",
             summary="Запустить генерацию вариантов для экзамена")
async def generate_variants(
    exam_id: str,
    variants_count: int = Query(5, ge=2, le=30),
    db: Session = Depends(get_db),
):
    exam = ExamRepository(db).get_by_id(exam_id)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")
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
            "templatized_tasks": report.templatized_count,
            "original_tasks": report.original_count,
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
    pass_rate_pct: float  # % scored >= 50%
    skill_gaps: List[dict] = []


@router.get("/exams/{exam_id}/analytics", response_model=ExamAnalyticsResponse,
            summary="Аналитика экзамена (учитель)")
async def get_exam_analytics(exam_id: str, db: Session = Depends(get_db)):
    exam = ExamRepository(db).get_by_id(exam_id)
    if not exam:
        raise HTTPException(404, "Экзамен не найден")

    sessions = (
        db.query(ExamSession)
        .filter(ExamSession.exam_id == exam_id)
        .all()
    )
    total = len(sessions)
    submitted = [s for s in sessions if s.status == "submitted"]
    n_submitted = len(submitted)

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
    task_names: Dict[str, str] = {}

    # Build correct-answer lookup from exam variants
    task_lookup: Dict[str, dict] = {}
    for v in (exam.variants or []):
        for t in v.get("tasks", []):
            task_lookup[t["task_id"]] = t

    for s in submitted:
        for task_id, student_ans in (s.answers or {}).items():
            task = task_lookup.get(task_id, {})
            correct_ans = task.get("correct_answer", "")
            task_total[task_id] = task_total.get(task_id, 0) + 1
            if _check_answer(student_ans, correct_ans):
                task_correct[task_id] = task_correct.get(task_id, 0) + 1
            task_names[task_id] = task.get("skill_name", "")

    skill_gaps = []
    for task_id, total_attempts in task_total.items():
        correct = task_correct.get(task_id, 0)
        rate = correct / total_attempts
        skill_gaps.append({
            "task_id": task_id,
            "skill_name": task_names.get(task_id, ""),
            "correct_rate": round(rate, 3),
            "attempts": total_attempts,
        })
    # Sorted by worst performance first
    skill_gaps.sort(key=lambda x: x["correct_rate"])

    return ExamAnalyticsResponse(
        exam_id=exam_id,
        title=exam.title,
        total_sessions=total,
        submitted_sessions=n_submitted,
        avg_score_pct=round(avg_pct, 1),
        avg_time_seconds=round(avg_time, 1),
        pass_rate_pct=round(pass_rate, 1),
        skill_gaps=skill_gaps,
    )
