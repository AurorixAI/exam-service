"""
Exam Service — Shared read-only ORM models.

These tables are owned by the platform / monolith.
Exam-service only reads from them.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy import JSON as JSONB
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


# ── knowledge_hierarchy (L1→L4 curriculum tree) ────────────────────────────

class KnowledgeHierarchy(Base):
    __tablename__ = "knowledge_hierarchy"

    id = Column(String(30), primary_key=True)
    level = Column(String(10), nullable=False)          # L1 / L2 / L3 / L4
    parent_id = Column(String(30), ForeignKey("knowledge_hierarchy.id"))
    name = Column(String(200))
    name_ru = Column(String(200))
    description = Column(Text)
    sequence_order = Column(Integer, default=0)
    class_level_start = Column(Integer)
    class_level_end = Column(Integer)
    cognitive_type = Column(String(30))

    # L4-specific
    difficulty_level = Column(String(30))
    importance = Column(Integer, default=5)
    typical_time_seconds = Column(Integer)
    example_task = Column(Text)
    assessed_ability = Column(Text)
    formula = Column(Text)

    # IRT calibration (L4)
    irt_discrimination = Column(Numeric(6, 4), default=1.0)
    irt_difficulty = Column(Numeric(6, 4), default=0.0)
    irt_guessing = Column(Numeric(6, 4), default=0.0)

    tasks = relationship("TaskMaster", back_populates="skill")
    parent = relationship(
        "KnowledgeHierarchy",
        back_populates="children",
        foreign_keys="[KnowledgeHierarchy.parent_id]",
        remote_side="KnowledgeHierarchy.id",
        uselist=False,
    )
    children = relationship(
        "KnowledgeHierarchy",
        back_populates="parent",
        foreign_keys="[KnowledgeHierarchy.parent_id]",
    )


# ── tasks_master (task bank) ───────────────────────────────────────────────

class TaskMaster(Base):
    __tablename__ = "tasks_master"

    id = Column(String(30), primary_key=True)
    skill_id = Column(String(30), ForeignKey("knowledge_hierarchy.id"), nullable=True)
    toc_id = Column(Integer, ForeignKey("textbook_toc.id"), nullable=True)

    question_text = Column(Text, nullable=False)
    question_latex = Column(Text)
    question_image_url = Column(Text)

    answer_type = Column(String(20), nullable=False)
    correct_answer = Column(Text, nullable=False)
    correct_answer_latex = Column(Text)
    answer_tolerance = Column(Numeric(10, 6))
    answer_options = Column(JSONB)

    has_template = Column(Boolean, default=False)
    template_params = Column(JSONB)

    difficulty = Column(String(1), nullable=False, default="B")  # A / B / C
    difficulty_score = Column(Numeric(3, 2))
    cognitive_load = Column(String(20))

    irt_discrimination = Column(Numeric(6, 4), default=1.0)
    irt_difficulty = Column(Numeric(6, 4), default=0.0)
    irt_guessing = Column(Numeric(6, 4), default=0.0)

    solution_explanation = Column(Text)
    distractor_meta = Column(JSONB)
    sympy_solution = Column(Text)

    verification_status = Column(String(20), default="pending")

    times_used = Column(Integer, default=0)
    times_correct = Column(Integer, default=0)
    avg_time_seconds = Column(Numeric(10, 2))

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    tags = Column(JSONB, default=dict)

    skill = relationship("KnowledgeHierarchy", back_populates="tasks")
    toc_entry = relationship("TextbookToc", back_populates="tasks")


# ── task_templates (variant generation cache) ─────────────────────────────

class TaskTemplateDB(Base):
    __tablename__ = "task_templates"

    id = Column(String(30), primary_key=True)
    original_task_id = Column(String(30), ForeignKey("tasks_master.id"))
    skill_id = Column(String(30), ForeignKey("knowledge_hierarchy.id"))

    template_text = Column(Text)
    template_latex = Column(Text)
    parameters = Column(JSONB)
    constraints = Column(JSONB)
    answer_formula = Column(Text)

    # Extended fields
    task_type = Column(String(30))
    text_template = Column(Text)
    original_values = Column(JSONB)
    param_details = Column(JSONB)
    answer_formula_sympy = Column(Text)
    constraints_repr = Column(Text)

    generation_method = Column(String(20), default="llm")
    quality_score = Column(Float, default=0.0)
    tested_combinations = Column(Integer, default=0)
    successful_combinations = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, server_default=func.now())


# ── textbooks ──────────────────────────────────────────────────────────────

class Textbook(Base):
    __tablename__ = "textbooks"

    textbook_id = Column(String(36), primary_key=True)
    isbn = Column(String(20))
    title = Column(String(300), nullable=False)
    subtitle = Column(String(300))
    edition = Column(String(50))
    authors = Column(JSONB, default=list)
    publisher = Column(String(200))
    subject = Column(String(100))
    class_level = Column(Integer)
    country = Column(String(10), default="KZ")
    language = Column(String(10), default="ru")
    cover_image_url = Column(Text)
    total_pages = Column(Integer)
    digitization_status = Column(String(30), default="pending")
    digitization_progress = Column(Float, default=0.0)
    tasks_extracted = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    toc_entries = relationship("TextbookToc", back_populates="textbook")


# ── textbook_toc (table of contents) ─────────────────────────────────────

class TextbookToc(Base):
    __tablename__ = "textbook_toc"

    id = Column(Integer, primary_key=True, autoincrement=True)
    textbook_id = Column(String(36), ForeignKey("textbooks.textbook_id"))
    parent_id = Column(Integer, ForeignKey("textbook_toc.id"))
    level = Column(String(20))
    number = Column(String(30))
    title = Column(String(500), nullable=False)
    full_path = Column(Text)
    page_start = Column(Integer)
    page_end = Column(Integer)
    sort_order = Column(Integer, default=0)
    tasks_extracted = Column(Integer, default=0)
    is_digitized = Column(Boolean, default=False)

    textbook = relationship("Textbook", back_populates="toc_entries")
    parent = relationship(
        "TextbookToc",
        back_populates="children",
        foreign_keys="[TextbookToc.parent_id]",
        remote_side="TextbookToc.id",
        uselist=False,
    )
    children = relationship(
        "TextbookToc",
        back_populates="parent",
        foreign_keys="[TextbookToc.parent_id]",
    )
    tasks = relationship("TaskMaster", back_populates="toc_entry")
    skill_maps = relationship("TextbookSkillMap", back_populates="toc_entry")


# ── textbook_skill_map ─────────────────────────────────────────────────────

class TextbookSkillMap(Base):
    __tablename__ = "textbook_skill_map"

    id = Column(Integer, primary_key=True, autoincrement=True)
    toc_id = Column(Integer, ForeignKey("textbook_toc.id"))
    skill_id = Column(String(30), ForeignKey("knowledge_hierarchy.id"))
    role = Column(String(20), default="primary")   # primary / secondary / prerequisite
    confidence = Column(Float, default=0.8)
    is_main = Column(Boolean, default=False)

    toc_entry = relationship("TextbookToc", back_populates="skill_maps")
