"""Exam Service — Redis client singleton"""
from __future__ import annotations

import redis

from src.core.config import get_settings

_client: redis.Redis | None = None


def get_redis_client() -> redis.Redis:
    """Return module-level singleton Redis client."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _client
