"""Abuse limits for the PUBLIC, unauthenticated exam entry (ROADMAP #33).

Anyone who holds an exam code (it is on the board and in the class chat) can ask «is this ID / name in the
group?» and gets the student's name back on a match, so the IDs TE-001..TE-200 can be walked through and the
group roster read.  The answer cannot be withheld (the page shows the confirmed student), so the walk is made
expensive instead, without locking out a legitimate class:

* A whole class sits behind ONE school NAT and joins in the same minute (30 students x check + start).  So the
  strict budget counts DISTINCT FAILED INPUTS (not found / ambiguous / name mismatch) - retyping the same wrong ID
  or name counts once, a legitimate student fails rarely, a walk over the ID space fails on every guess - and it
  is kept per (exam code, client address), never per address alone.  The volume of ALL attempts is capped far
  above what a class does, only to stop floods.
* The budget is taken at ADMISSION, atomically (a provisional failure added to a set under the limit in one Redis
  step) and given back when the attempt resolves a student, so parallel requests cannot overshoot it.
* A distributed walk (many addresses, one code) is met by a per-code circuit breaker that does NOT lock the code:
  once the code has collected CODE_FAILS_SOFT distinct failures in an hour, only an address that is itself
  walking is refused - at least BREAKER_MIN_DISTINCT_FAILS failures AND more than half of its attempts failing.
  A class behind a NAT with a few typos stays in.
* The teacher can clear all of this for an exam (``reset_entry_limits``): every key carries a per-code epoch
  number, a reset bumps it, and the old keys are simply never read again (they expire by themselves).

Counters are Redis (shared by the uvicorn workers).  While Redis is unreachable they run on per-process memory with
ONE warning per outage (fail open: availability first, the limits just stop being shared).

The client address: nginx overwrites X-Real-IP with $remote_addr on the exam locations, so that header is the
only one trusted.  X-Forwarded-For is NOT: nginx does not set it there, so its content is whatever the client
typed.  Without X-Real-IP the request did not come through our proxy and the socket peer is the caller.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import re
import threading
import time
import unicodedata
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
# DISTINCT failed inputs.  A legitimate class has a handful (typos, retyped once); a walk has one per guess.
FAILS_PER_MINUTE = Limit(30, 60)
FAILS_PER_HOUR = Limit(150, 3600)
# Per exam code over ALL addresses: when this many distinct failures were seen in an hour the breaker is armed.
# It is above one address's hour budget on purpose: a lone walker meets its own budget first, the breaker is for
# several addresses walking the same code...
CODE_FAILS_SOFT = Limit(200, 3600)
# ...and refuses only an address that is walking: this many distinct failures and more than this share of attempts.
BREAKER_MIN_DISTINCT_FAILS = 30
BREAKER_FAIL_RATIO = 0.5


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


_WHITESPACE = re.compile(r"\s+")


def identity_key(*typed: Optional[str]) -> str:
    """The typed identity (ID / name fields) normalised and hashed: retyping the same input is the same key.

    NFKC + case-folding + collapsed spaces only.  It is deliberately NOT as forgiving as auth-service's matching
    (that one equates more spellings): merging spellings auth tells apart would let one input stand for several.
    """
    parts = [
        _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", part or "").casefold()).strip() for part in typed
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]


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

# Add a member to a set unless the set is full - ONE atomic step, so parallel requests cannot overshoot the limit.
# Returns {status, size, ttl}: 0 = already there (counts once), 1 = added, 2 = full (not added).
_ADD_LUA = """
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then
  return {0, redis.call('SCARD', KEYS[1]), redis.call('TTL', KEYS[1])}
end
local n = redis.call('SCARD', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if n >= tonumber(ARGV[3]) then
  return {2, n, ttl}
end
redis.call('SADD', KEYS[1], ARGV[1])
if ttl < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  ttl = tonumber(ARGV[2])
end
return {1, n + 1, ttl}
"""

EXISTS, ADDED, FULL = 0, 1, 2

_EPOCH_TTL = 7 * 24 * 3600  # an epoch outlives every window it guards (the longest is an hour)


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
        self._sets: dict[str, list] = {}  # key -> [set of members, ends_at]
        self._epochs: dict[str, int] = {}
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

    # -- counters: INCR in a fixed window ----------------------------------------------------------------
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

    # -- sets of distinct members ------------------------------------------------------------------------
    def add_member(self, key: str, member: str, window: int, limit: int) -> Tuple[int, int, int]:
        """Add *member* to the set at *key* unless it already holds *limit* members, atomically.

        (EXISTS | ADDED | FULL, members now, seconds left).  A member that is already there counts once.
        """
        client = self._redis()
        if client is not None:
            try:
                status, size, ttl = client.eval(_ADD_LUA, 1, key, member, window, limit)
                self._mark_up()
                return int(status), int(size), max(int(ttl), 1)
            except Exception as exc:  # noqa: BLE001
                self._mark_down(exc)
        now = _now()
        with self._lock:
            members, ends_at = self._sets.get(key, (set(), 0.0))
            if ends_at <= now:
                members, ends_at = set(), now + window
            left = max(math.ceil(ends_at - now), 1)
            if member in members:
                return EXISTS, len(members), left
            if len(members) >= limit:
                return FULL, len(members), left
            members.add(member)
            self._sets[key] = [members, ends_at]
            self._prune(now)
            return ADDED, len(members), left

    def remove_member(self, key: str, member: str) -> None:
        client = self._redis()
        if client is not None:
            try:
                client.srem(key, member)
                self._mark_up()
                return
            except Exception as exc:  # noqa: BLE001
                self._mark_down(exc)
        with self._lock:
            entry = self._sets.get(key)
            if entry:
                entry[0].discard(member)

    def count_members(self, key: str) -> Tuple[int, int]:
        """(members in the set, seconds left) without changing it; (0, 0) when there is none."""
        client = self._redis()
        if client is not None:
            try:
                pipe = client.pipeline()
                pipe.scard(key)
                pipe.ttl(key)
                size, ttl = pipe.execute()
                self._mark_up()
                return (int(size), max(int(ttl), 1)) if int(size) else (0, 0)
            except Exception as exc:  # noqa: BLE001
                self._mark_down(exc)
        now = _now()
        with self._lock:
            members, ends_at = self._sets.get(key, (set(), 0.0))
            size = len(members)
        if ends_at <= now or not size:
            return 0, 0
        return size, max(math.ceil(ends_at - now), 1)

    # -- epochs: the teacher's reset ---------------------------------------------------------------------
    def epoch(self, scope: str) -> int:
        client = self._redis()
        if client is not None:
            try:
                value = client.get(f"examepoch:{scope}")
                self._mark_up()
                return int(value) if value is not None else 0
            except Exception as exc:  # noqa: BLE001
                self._mark_down(exc)
        with self._lock:
            return self._epochs.get(scope, 0)

    def bump_epoch(self, scope: str) -> int:
        client = self._redis()
        if client is not None:
            try:
                key = f"examepoch:{scope}"
                value = int(client.incr(key))
                client.expire(key, _EPOCH_TTL)
                self._mark_up()
                return value
            except Exception as exc:  # noqa: BLE001
                self._mark_down(exc)
        with self._lock:
            self._epochs[scope] = self._epochs.get(scope, 0) + 1
            return self._epochs[scope]

    def _prune(self, now: float) -> None:
        """Keep a flood of fresh keys from growing process memory (caller holds the lock)."""
        if len(self._memory) + len(self._sets) <= _MEMORY_SOFT_LIMIT:
            return
        for store in (self._memory, self._sets):
            for stale in [k for k, (_, end) in store.items() if end <= now]:
                del store[stale]
            if len(store) > _MEMORY_HARD_LIMIT:
                oldest = sorted(store.items(), key=lambda item: item[1][1])
                for key, _ in oldest[: len(oldest) - _MEMORY_HARD_LIMIT // 2]:
                    del store[key]

    def reset(self) -> None:
        """Tests: forget every counter and every Redis decision."""
        with self._lock:
            self._memory.clear()
            self._sets.clear()
            self._epochs.clear()
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
    """One admitted identity attempt, holding a provisional failure until it resolves.

    ``fail()`` makes the failure real (the lookup found nobody); ``close()`` - always called, in a ``finally`` -
    gives the provisional failure back unless ``fail()`` was called: a match, a 410, a 503 are not failed guesses.
    """

    def __init__(self, ip_scope: str, code_scope: str, member: str, fail_keys: Tuple[str, str], code_key_: str) -> None:
        self._ip_scope = ip_scope
        self._code_scope = code_scope
        self._member = member
        self._fail_keys = fail_keys
        self._code_key = code_key_
        self._failed = False
        self._closed = False

    def fail(self) -> None:
        if self._failed:
            return
        self._failed = True
        # the per-code breaker counts each (address, input) once
        counters.add_member(
            self._code_key,
            hashlib.sha256(f"{self._ip_scope}|{self._member}".encode("utf-8")).hexdigest()[:24],
            CODE_FAILS_SOFT.window,
            10**9,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._failed:
            for key in self._fail_keys:
                counters.remove_member(key, self._member)


def reset_entry_limits(exam_code: str) -> int:
    """Teacher's reset: every counter of this exam code starts again from zero.  Returns the new epoch."""
    return counters.bump_epoch(code_key(exam_code))


def admit_identity_attempt(request: Request, exam_code: str, *typed_identity: Optional[str]) -> EntryTicket:
    """Admit (or refuse with 429 + Retry-After) one check-student / start attempt for *exam_code*.

    *typed_identity* is what the student typed (ID / name fields): the failure budget counts distinct inputs.
    Every window is evaluated, and the refusal carries the LONGEST wait of the ones that are exceeded.
    """
    code_scope = code_key(exam_code)
    epoch = counters.epoch(code_scope)
    ip_scope = f"{code_scope}:{epoch}:{client_bucket(request)}"
    member = identity_key(*typed_identity)
    fail_keys = tuple(f"examfail:{limit.window}:{ip_scope}" for limit in (FAILS_PER_MINUTE, FAILS_PER_HOUR))
    code_fail_key = f"examcodefail:{code_scope}:{epoch}"
    exceeded: list[int] = []

    # the breaker: armed by the code's distinct failures, aimed at an address that is itself walking
    code_failures, _ = counters.count_members(code_fail_key)
    if code_failures >= CODE_FAILS_SOFT.count:
        ip_failures, left = counters.count_members(fail_keys[1])
        attempts, _ = counters.peek(f"examtry:{ATTEMPTS_PER_HOUR.window}:{ip_scope}")
        if ip_failures >= BREAKER_MIN_DISTINCT_FAILS and attempts and ip_failures / attempts > BREAKER_FAIL_RATIO:
            exceeded.append(left)

    for limit in (ATTEMPTS_PER_MINUTE, ATTEMPTS_PER_HOUR):
        attempts, left = counters.hit(f"examtry:{limit.window}:{ip_scope}", limit.window)
        if attempts > limit.count:
            exceeded.append(left)

    # the provisional failure: taken atomically under the budget, given back on a match (EntryTicket.close)
    added = []
    for key, limit in zip(fail_keys, (FAILS_PER_MINUTE, FAILS_PER_HOUR)):
        status, _, left = counters.add_member(key, member, limit.window, limit.count)
        if status == FULL:
            exceeded.append(left)
        elif status == ADDED:
            added.append(key)

    if exceeded:
        for key in added:
            counters.remove_member(key, member)
        raise too_many_attempts(max(exceeded))

    return EntryTicket(ip_scope, code_scope, member, fail_keys, code_fail_key)
