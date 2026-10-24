"""Alembic env.py — manages only exam service tables."""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.db_timezone import utc_connect_args  # noqa: E402  (migrations run in the same UTC session as the service)
from src.models.exam_models import (  # noqa: F401
    Base, ExamDeletion, ExamLifecycleEvent, ExamPrintPackage, ExamReport, GeneratedExam, ExamSession,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Allow DATABASE_URL env var to override alembic.ini value
db_url = os.getenv("DATABASE_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

target_metadata = Base.metadata

# Only manage exam service owned tables — never touch shared/monolith tables
_MANAGED = {
    "generated_exams", "exam_sessions", "exam_lifecycle_events", "exam_deletions", "exam_print_packages", "exam_reports",
}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in _MANAGED
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=utc_connect_args(config.get_main_option("sqlalchemy.url")),
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
