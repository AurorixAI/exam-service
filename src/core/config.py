"""Exam Service — Configuration"""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_name: str = "ALGO Exam Service"
    app_version: str = "1.0.0"
    app_env: str = Field(default="production", pattern="^(development|staging|production|testing)$")
    debug: bool = False

    # Database — own DB for exams/results
    database_url: str = "postgresql://algo:algo_password@localhost:5432/algo_v1"
    # Database pool and server-side safety nets (ROADMAP #95).  Per uvicorn worker: a worker serves at most
    # ``worker_threads`` blocking calls at once (every DB handler runs in the thread pool), so pool_size + max_overflow
    # is sized to that, not to the 15 connections that starved a 30-student class.  The three timeouts are set on EVERY
    # connection by the server: a lock wait, a statement and a transaction that does nothing are all bounded, so a
    # stuck request turns into a 503 the student's browser retries instead of silence.
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 10  # seconds a request waits for a free connection before it gets a 503
    db_lock_timeout_ms: int = 5000
    db_statement_timeout_ms: int = 15000
    db_idle_in_transaction_timeout_ms: int = 30000
    # Size of the thread pool FastAPI/Starlette runs plain ``def`` handlers and ``run_in_threadpool`` in (per worker).
    worker_threads: int = 40
    # Staff (teacher/admin) requests in flight per worker.  They wait on auth-service with a thread in hand, so they get
    # their own cap: a flood of dashboard polls is answered 503 + Retry-After instead of taking the threads and the
    # connections the students of a running exam need.
    teacher_slots: int = 16
    # ...of which this many are kept for changes (close, start, assign...): a flood of dashboard reads fills the
    # others first, so the teacher who presses «close» during it is still served.
    teacher_write_reserve: int = 4
    # Content Service — HTTP read API (replaces direct content-db connection)
    content_service_url: str = "http://content-service:8004"
    # Auth Service — HTTP API for group/student verification
    auth_service_url: str = "http://auth-service:8000"
    # Shared only by first-party services.  It authenticates the cancellation
    # notification from auth-service; browser credentials are never accepted
    # on that internal route.
    internal_service_token: str = ""
    # Key of the HMAC the public entry limits hash typed IDs/names with (src/core/abuse_limit.py).  Empty: derived from
    # INTERNAL_SERVICE_TOKEN; with neither (development) a random per-process key and a warning.
    entry_limit_secret: str = ""

    # Redis — template cache
    redis_url: str = "redis://localhost:6379"
    redis_template_cache_ttl: int = 3600  # 1 hour

    # Gemini AI — variant templatization / fallback
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # Azure OpenAI / DeepSeek API (Primary AI Engine)
    azure_deepseek_api_key: str = ""
    azure_deepseek_endpoint: str = ""
    azure_deepseek_model: str = "deepseek-v4-flash"

    # Exam parameters
    max_tasks_per_exam: int = 50
    max_variants: int = 30
    default_time_minutes: int = 45

    # CORS
    cors_origins: List[str] = ["http://localhost:3000", "http://localhost:3001", "http://127.0.0.1:3000"]

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
