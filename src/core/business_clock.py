"""TIME-TESTS (P2): the exam service reads the controllable clock of the end-to-end stand.

The stand shifts the business time by an offset in seconds kept in Redis (``algo:test:clock_offset_seconds``); the auth service WRITES it
(``PUT /api/v1/auth/dev/clock``), this service only READS it, so both services live by the same business time.  The mode is decided ONCE, at
import: ``APP_ENV=development`` EXACTLY and ``ALGO_TEST_CLOCK=1``.  ``ALGO_TEST_CLOCK`` with any non-empty value on another ``APP_ENV`` stops the
start (``check_test_clock_allowed``, run at import).  Outside the mode the offset is 0 at once: no Redis call, no lock.  In the mode any failure to
read the offset (no address, Redis down, not a number) is a loud ``TestClockError``, never a silent zero.

Everything the service reads as "now" shifts together: the business day, the row stamps (``created_at``, ``occurred_at``, ``started_at`` ...), the timers
of a running exam, the cool-downs.  A shifted day is compared only with shifted stamps.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Mapping, Optional

TEST_CLOCK_VARIABLE = "ALGO_TEST_CLOCK"
TEST_CLOCK_REDIS_VARIABLE = "ALGO_TEST_CLOCK_REDIS_URL"
OFFSET_KEY = "algo:test:clock_offset_seconds"  # the same key as in auth-service
OFFSET_CACHE_SECONDS = 0.2


def is_dev_env(environ: Optional[Mapping[str, str]] = None) -> bool:
    """``APP_ENV`` equals ``development`` EXACTLY (no trimming, no case folding)."""
    env = os.environ if environ is None else environ
    return (env.get("APP_ENV") or "") == "development"


def check_test_clock_allowed(environ: Optional[Mapping[str, str]] = None) -> None:
    """``ALGO_TEST_CLOCK`` with ANY non-empty value outside ``APP_ENV=development`` stops the start."""
    env = os.environ if environ is None else environ
    if (env.get(TEST_CLOCK_VARIABLE) or "").strip() and not is_dev_env(env):
        raise RuntimeError(
            f"CRITICAL: {TEST_CLOCK_VARIABLE} задан при APP_ENV={(env.get('APP_ENV') or '')!r}: управляемые часы допустимы только на сквозном стенде "
            f"(APP_ENV=development И {TEST_CLOCK_VARIABLE}=1). Уберите {TEST_CLOCK_VARIABLE} (и {TEST_CLOCK_REDIS_VARIABLE}) из окружения этого сервера."
        )


check_test_clock_allowed()  # the service does not start with the switch on a deployed stack
TEST_CLOCK_MODE: bool = is_dev_env() and (os.environ.get(TEST_CLOCK_VARIABLE) or "").strip() == "1"  # decided once; the only gate of the offset code

_offset_lock = threading.Lock()
_offset_cache: tuple[int, float] = (0, 0.0)  # (seconds, valid until a ``time.monotonic()`` value)
_redis_client = None


class TestClockError(RuntimeError):
    """The test clock is on but cannot be read: loud, never a silent zero."""

    __test__ = False  # not a pytest class


def _redis():
    """The client of the clock's Redis; created lazily, only under ``_offset_lock``."""
    global _redis_client
    if _redis_client is None:
        url = (os.environ.get(TEST_CLOCK_REDIS_VARIABLE) or "").strip()
        if not url:
            raise TestClockError(f"{TEST_CLOCK_VARIABLE}=1, но не задан {TEST_CLOCK_REDIS_VARIABLE}: нет адреса Redis со смещением часов")
        import redis

        _redis_client = redis.Redis.from_url(url, socket_connect_timeout=1.0, socket_timeout=1.0, decode_responses=True)
    return _redis_client


def _read_offset() -> int:
    try:
        raw = _redis().get(OFFSET_KEY)
    except TestClockError:
        raise
    except Exception as exc:  # noqa: BLE001 - in the test mode any Redis failure stops the request instead of becoming "no shift"
        raise TestClockError(f"управляемые часы: Redis недоступен ({type(exc).__name__}: {exc})") from exc
    if raw in (None, ""):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise TestClockError(f"управляемые часы: в {OFFSET_KEY} не целое число ({raw!r})") from exc


def current_offset_seconds() -> int:
    """The shift of the stand clock in seconds.  Outside the test mode: 0, at once, with no Redis call and no lock."""
    global _offset_cache
    if not TEST_CLOCK_MODE:
        return 0
    with _offset_lock:
        value, valid_until = _offset_cache
        now = time.monotonic()
        if now < valid_until:
            return value
        value = _read_offset()
        _offset_cache = (value, now + OFFSET_CACHE_SECONDS)
        return value


def utc_now() -> datetime:
    """Now as a naive UTC instant (the type of every UTC column) on the stand clock: real time + the shared offset.

    Outside the test mode this is exactly ``datetime.utcnow()`` by value and type.  EVERY read of the time in ``src`` goes through here (or through
    ``business_now``): a shifted business day is compared only with shifted stamps and timers (guard: ``tests/test_time_clock.py``)."""
    moment = datetime.now(timezone.utc).replace(tzinfo=None)
    offset = current_offset_seconds()  # 0 and no Redis call outside the test mode
    return moment + timedelta(seconds=offset) if offset else moment


def business_now(zone: tzinfo) -> datetime:
    """Now, aware, in ``zone`` on the stand clock."""
    moment = datetime.now(zone)
    offset = current_offset_seconds()
    return moment + timedelta(seconds=offset) if offset else moment


def utc_now_aware() -> datetime:
    """Now as an AWARE UTC instant on the stand clock (``datetime.now(timezone.utc)`` outside the test mode): for the columns that are
    ``timestamp with time zone`` and the places that always wrote an aware value."""
    return utc_now().replace(tzinfo=timezone.utc)
