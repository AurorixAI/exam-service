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
    # Content Service — HTTP read API (replaces direct content-db connection)
    content_service_url: str = "http://content-service:8004"

    # Redis — template cache
    redis_url: str = "redis://localhost:6379"
    redis_template_cache_ttl: int = 3600  # 1 hour

    # Gemini AI — variant templatization
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

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
