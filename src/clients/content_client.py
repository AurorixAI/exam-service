"""
Exam Service — HTTP client for content-service read API.

Provides a singleton httpx.Client that task/textbook repositories use
instead of a direct Postgres connection to content-db.

Retry policy (tenacity):
  - 3 attempts with exponential backoff (1s, 2s, 4s)
  - Retries on ConnectError and TimeoutException only
"""
from __future__ import annotations

import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from src.core.config import get_settings

log = logging.getLogger(__name__)

_client: httpx.Client | None = None


def get_client() -> httpx.Client:
    """Return (or lazily create) the shared HTTP client for content-service."""
    global _client
    if _client is None:
        s = get_settings()
        _client = httpx.Client(
            base_url=s.content_service_url,
            timeout=10.0,
            headers={
                "Accept": "application/json",
                "X-Internal-Service-Token": s.internal_service_token,
            },
        )
    return _client


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def get(path: str, **kwargs) -> httpx.Response:
    """GET with automatic retry on transient network errors."""
    return get_client().get(path, **kwargs)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def post(path: str, **kwargs) -> httpx.Response:
    """POST with automatic retry on transient network errors."""
    return get_client().post(path, **kwargs)
