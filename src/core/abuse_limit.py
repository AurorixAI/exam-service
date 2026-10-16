"""Abuse limits for the PUBLIC, unauthenticated exam entry (ROADMAP #33).

Anyone who holds an exam code (it is on the board and in the class chat) can ask «is this ID / name in the
group?» and gets the student's name back on a match, so the IDs TE-001..TE-200 can be walked through and the
group roster read.  The answer cannot be withheld (the page shows the confirmed student), so the walk is made
expensive instead, without locking out a legitimate class:

* A whole class sits behind ONE school NAT and joins in the same minute (30 students x check + start).  So the
  strict limits count FAILED lookups only (not found / ambiguous / name mismatch) - a legitimate student fails
  rarely, a walk over the ID space fails all the time - and they are kept per (exam code, client address), never
  per address alone: two classes behind one NAT with different codes do not share a budget.  The volume of ALL
  attempts is capped far above what a class does, only to stop floods.
* A distributed walk (many addresses, one code) is met by a per-code circuit breaker that does NOT lock the code:
  once the code has collected CODE_FAILS_SOFT failures in an hour, only addresses that already failed
  SUSPECT_IP_FAILS times are refused; an address that has not failed (a student on mobile data) still enters.

Counters are fixed windows in Redis (shared by the uvicorn workers).  While Redis is unreachable they run on
per-process memory with ONE warning per outage (fail open: availability first, the limits just stop being shared).

The client address: nginx overwrites X-Real-IP with $remote_addr on the exam locations, so that header is the
only one trusted.  X-Forwarded-For is NOT: nginx does not set it there, so its content is whatever the client
typed.  Without X-Real-IP the request did not come through our proxy and the socket peer is the caller.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import redis
from fastapi import HTTPException, Request

from src.core.config import get_settings

log = logging.getLogger("exam_service.abuse_limit")

MSG_TOO_MANY_ATTEMPTS = "Слишком много попыток. Подождите минуту и попробуйте снова."


@dataclass(frozen=True)
class Limit:
    count: int
    window: int  # seconds


# ── the policy (per exam code AND client address unless noted) ─────────────────────────────────────────────
# All identity attempts (check-student + start).  A class of 40 does ~80 per minute, ~120 per hour with reloads.
ATTEMPTS_PER_MINUTE = Limit(120, 60)
ATTEMPTS_PER_HOUR = Limit(400, 3600)
# Failed lookups only.  A legitimate class has a handful (typos); a walk over the ID space has one per guess.
FAILS_PER_MINUTE = Limit(15, 60)
FAILS_PER_HOUR = Limit(60, 3600)
# Per exam code over ALL addresses: the circuit breaker described in the module docstring.
CODE_FAILS_SOFT = Limit(100, 3600)
SUSPECT_IP_FAILS = 10


# ── client address ─────────────────────────────────────────────────────────────────────────────────────────
IPV6_BUCKET_PREFIX_LENGTH = 64


def _valid_ip(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    """X-Real-IP (set by our nginx from the socket), else the socket peer.  Never X-Forwarded-For."""
    real_ip = _valid_ip(request.headers.get("x-real-ip"))
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


def limit_bucket(address: str) -> str:
    """IPv4 as it is; IPv6 by its /64 (one subscriber owns 2**64 addresses); an IPv4-mapped IPv6 as its IPv4."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network(f"{ip}/{IPV6_BUCKET_PREFIX_LENGTH}", strict=False))


def client_bucket(request: Request) -> str:
    return limit_bucket(client_ip(request))


def code_key(code: str) -> str:
    """The code as a short fixed-size key: what the client typed never becomes a key (length, characters)."""
    return hashlib.sha256((code or "").strip().upper().encode("utf-8")).hexdigest()[:24]


# ── counters ───────────────────────────────────────────────────────────────────────────────────────────────
# INCR + EXPIRE as ONE atomic step; later hits do not push the TTL out (a client that keeps retrying is let
# back in when the window ends).
_HIT_LUA = """
local c = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if c == 1 or ttl < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {c, ttl}
"""

def _now() -> float:
    return time.monotonic()


_REDIS_RETRY_AFTER = 30.0  # seconds without trying Redis after it failed
_REDIS_TIMEOUT = 0.25  # a counter must never hold a request (these endpoints are async: it blocks the loop)
_MEMORY_SOFT_LIMIT = 20_000
_MEMORY_HARD_LIMIT = 100_000


class _Counters:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._memory: dict[str, list] = {}  # key -> [count, ends_at (monotonic)]
        self._client: Optional[redis.Redis] = None
        self._down_until = 0.0
        self._warned = False

    # -- Redis availability ------------------------------------------------------------------------------
    def _redis(self) -> Optional[redis.Redis]:
        if _now() < self._down_until:
            return None
        if self._client is None:
            settings = get_settings()
            if settings.app_env == "testing":
                return None  # the suite never talks to a real Redis; a test that wants one injects it (use_redis)
            self._client = redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=_REDIS_TIMEOUT,
                socket_timeout=_REDIS_TIMEOUT,
            )
        return self._client

    def _mark_down(self, exc: Exception) -> None:
        self._down_until = _now() + _REDIS_RETRY_AFTER
        if not self._warned:
            self._warned = True
            log.warning(
                "abuse_limit_redis_unavailable error_type=%s: exam entry limits run on per-process memory "
                "(not shared between workers) until Redis is back",
                type(exc).__name__,
            )

    def _mark_up(self) -> None:
        if self._warned:
            log.info("abuse_limit_redis_recovered")
        self._warned = False

    def use_redis(self, client: Optional[redis.Redis]) -> None:
        """Tests: count in this client (None = back to the default selection)."""
        self._client = client
        self._down_until = 0.0

    # -- operations --------------------------------------------------------------------------------------
    def hit(self, key: str, window: int) -> Tuple[int, int]:
        """Count one event in a fixed window: (events so far including this one, seconds left)."""
        client = self._redis()
        if client is not None:
            try:
                count, ttl = client.eval(_HIT_LUA, 1, key, window)
                self._mark_up()
                return int(count), max(int(ttl), 1)
            except Exception as exc:  # noqa: BLE001 - a counter store must never break a request
                self._mark_down(exc)
        now = _now()
        with self._lock:
            count, ends_at = self._memory.get(key, (0, 0.0))
            if ends_at <= now:
                count, ends_at = 0, now + window
            count += 1
            self._memory[key] = [count, ends_at]
            self._prune(now)
        return count, max(math.ceil(ends_at - now), 1)

    def peek(self, key: str) -> Tuple[int, int]:
        """(events in the current window, seconds left) without counting; (0, 0) when there is no window."""
        client = self._redis()
        if client is not None:
            try:
                pipe = client.pipeline()
                pipe.get(key)
                pipe.ttl(key)
                value, ttl = pipe.execute()
                self._mark_up()
                return (int(value), max(int(ttl), 1)) if value is not None else (0, 0)
            except Exception as exc:  # noqa: BLE001
                self._mark_down(exc)
        now = _now()
        with self._lock:
            count, ends_at = self._memory.get(key, (0, 0.0))
        if ends_at <= now:
            return 0, 0
        return count, max(math.ceil(ends_at - now), 1)

    def _prune(self, now: float) -> None:
        """Keep a flood of fresh keys from growing process memory (caller holds the lock)."""
        if len(self._memory) <= _MEMORY_SOFT_LIMIT:
            return
        for stale in [k for k, (_, end) in self._memory.items() if end <= now]:
            del self._memory[stale]
        if len(self._memory) > _MEMORY_HARD_LIMIT:
            oldest = sorted(self._memory.items(), key=lambda item: item[1][1])
            for key, _ in oldest[: len(oldest) - _MEMORY_HARD_LIMIT // 2]:
                del self._memory[key]

    def reset(self) -> None:
        """Tests: forget every counter and every Redis decision."""
        with self._lock:
            self._memory.clear()
        self._client = None
        self._down_until = 0.0
        self._warned = False


counters = _Counters()


# ── the 429 ────────────────────────────────────────────────────────────────────────────────────────────────
def too_many_attempts(retry_after: int) -> HTTPException:
    """429 with Retry-After.  Up to a minute and a half the text is the agreed one; a longer wait says how long."""
    retry_after = max(int(retry_after), 1)
    if retry_after <= 90:
        detail = MSG_TOO_MANY_ATTEMPTS
    else:
        detail = f"Слишком много попыток. Подождите {math.ceil(retry_after / 60)} мин и попробуйте снова."
    return HTTPException(status_code=429, detail=detail, headers={"Retry-After": str(retry_after)})


# ── the exam entry policy ──────────────────────────────────────────────────────────────────────────────────
class EntryTicket:
    """One admitted identity attempt.  Call ``fail()`` when the lookup did not resolve a student."""

    def __init__(self, ip_scope: str, code_scope: str) -> None:
        self._ip_scope = ip_scope
        self._code_scope = code_scope

    def fail(self) -> None:
        counters.hit(f"examfail:{FAILS_PER_MINUTE.window}:{self._ip_scope}", FAILS_PER_MINUTE.window)
        counters.hit(f"examfail:{FAILS_PER_HOUR.window}:{self._ip_scope}", FAILS_PER_HOUR.window)
        counters.hit(f"examcodefail:{self._code_scope}", CODE_FAILS_SOFT.window)


def admit_identity_attempt(request: Request, exam_code: str) -> EntryTicket:
    """Admit (or refuse with 429 + Retry-After) one check-student / start attempt for *exam_code*.

    Order matters: a spent failure budget refuses before anything else is counted, so a refused request does not
    keep the window open for the legitimate students behind the same address.
    """
    code_scope = code_key(exam_code)
    ip_scope = f"{code_scope}:{client_bucket(request)}"

    for limit in (FAILS_PER_MINUTE, FAILS_PER_HOUR):
        failures, left = counters.peek(f"examfail:{limit.window}:{ip_scope}")
        if failures >= limit.count:
            raise too_many_attempts(left)

    code_failures, _ = counters.peek(f"examcodefail:{code_scope}")
    if code_failures >= CODE_FAILS_SOFT.count:
        hour_failures, left = counters.peek(f"examfail:{FAILS_PER_HOUR.window}:{ip_scope}")
        if hour_failures >= SUSPECT_IP_FAILS:
            raise too_many_attempts(left)

    for limit in (ATTEMPTS_PER_MINUTE, ATTEMPTS_PER_HOUR):
        attempts, left = counters.hit(f"examtry:{limit.window}:{ip_scope}", limit.window)
        if attempts > limit.count:
            raise too_many_attempts(left)

    return EntryTicket(ip_scope, code_scope)
