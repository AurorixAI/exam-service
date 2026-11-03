"""AD-2b: a request that changes something on a STAFF route must come from a page of ALGO (the source check of exam-service).

The twin of ``auth-service/src/core/origin_guard.py`` (AD-2): the same decision, copied (there is no shared package between the repositories; the tables of
``tests/test_ad2b_origin_guard.py`` pin the two to the same behaviour).

The browser sends the staff cookie (``__Host-algo_auth_token``) on every request to our host, whoever's page started it. ``SameSite=Lax`` and the ``__Host-`` names
already keep a foreign page from riding on it; this check is the second wall, on the SERVER, against a page of a sister site (``*.algolab.me`` is one site for
SameSite) that runs a script: it must not be able to close somebody's live exam, publish a draft or delete an exam with the teacher's cookie.

**The decision** (``request_is_foreign``), in this order:

1. ``Sec-Fetch-Site: cross-site`` - every current browser sends it and a page's script cannot set it: foreign.
2. ``Origin`` present (every browser sends it on a POST/PUT/PATCH/DELETE; ``null`` of a sandboxed frame included): it must be one of the allowed origins, else
   foreign. A value that is not an origin at all is foreign.
3. No ``Origin`` but a ``Referer`` (an old browser; a privacy extension that strips Origin): the origin of the Referer must be allowed.
4. Neither header (curl, a server-to-server call, the Node side of the front): NOT judged. Such a request is not made by a browser page, so no foreign page can have
   started it with the visitor's cookies.

**Where it applies.** One ASGI middleware for every ``POST/PUT/PATCH/DELETE`` of the application EXCEPT the routes of ``EXEMPT`` (method + path template, one by one,
never a prefix). So a route written tomorrow is judged from its first day (a staff route cannot be forgotten), and the guard test asks a question about any route that
is neither a staff route nor listed here. The exempt routes are exactly the ones that read NO staff cookie:

* the student's routes (by exam code / by the session capability token): the live exam must not break when a student's page comes from another address (the old
  ``algolab.me`` alias, a bookmark, a Telegram in-app browser): a 403 there is the loss of the student's answers;
* ``/api/v1/internal/*``: service-to-service (auth-service), not a browser, protected by ``INTERNAL_SERVICE_TOKEN`` inside the handler.

**Allowed origins**: ``CORS_ORIGINS`` (the product origins; ``*`` = any, as in CORS; refused at the start of a deployed stack) PLUS the origin of ``FRONTEND_URL``
(LESSONS 68: the address the product is served from works even when the CORS list is one step behind - the product's requests to exam are same-origin through nginx, so the
CORS list of exam is exactly the one that lags unnoticed). ``FRONTEND_URL`` is optional here (no new mandatory variable): without it the list alone decides.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Callable, Mapping, Optional
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.core.config import get_settings

logger = logging.getLogger(__name__)

MUTATING_METHODS = frozenset(("POST", "PUT", "PATCH", "DELETE"))
BAD_ORIGIN_CODE = "bad_origin"
BAD_ORIGIN_TEXT = "Запрос должен приходить с сайта ALGO"

_INTERNAL = (
    "service-to-service (auth-service), not a browser: protected by the X-Internal-Service-Token header checked in the handler "
    "(_require_internal_service_token); no cookie is read"
)
_STUDENT_CODE = (
    "the student's entry by the exam code: anonymous, no cookie is read; held by the code, the entry rate limits and the roster check. "
    "A foreign Origin must pass: the live exam is opened from the old/alias address, a bookmark, an in-app browser"
)
_STUDENT_SESSION = (
    "the student's answers of a running attempt: held by the session capability token (X-Exam-Session-Token), no cookie is read. "
    "A 403 here is the loss of the student's answers, so a foreign Origin must pass"
)
# (METHOD, path template) -> why a foreign Origin is not judged there. ``{name}`` matches one path segment.
EXEMPT = {
    ("POST", "/api/v1/exams/by-code/{code}/check-student"): _STUDENT_CODE,
    ("POST", "/api/v1/exams/by-code/{code}/start"): _STUDENT_CODE,
    ("POST", "/api/v1/exam-sessions/{session_id}/progress"): _STUDENT_SESSION,
    ("POST", "/api/v1/exam-sessions/{session_id}/submit"): _STUDENT_SESSION,
    ("POST", "/api/v1/internal/lesson-bindings/cancellation-check"): _INTERNAL,
    ("POST", "/api/v1/internal/lesson-bindings/release"): _INTERNAL,
    ("POST", "/api/v1/internal/lesson-bindings/completion-check"): _INTERNAL,
    ("POST", "/api/v1/internal/lesson-bindings/completed"): _INTERNAL,
    ("POST", "/api/v1/internal/lesson-bindings/rescheduled"): _INTERNAL,
    ("POST", "/api/v1/internal/lesson-bindings/recurring-end-impact"): _INTERNAL,
    ("POST", "/api/v1/internal/lesson-bindings/recurring-end-release"): _INTERNAL,
    ("POST", "/api/v1/internal/group-bindings/archive-impact"): _INTERNAL,
    ("POST", "/api/v1/internal/group-bindings/archive-release"): _INTERNAL,
}
# Prefix whose mutating routes must ALL be listed above (the guard test asks); nothing is exempt by location.
EXEMPT_AREAS = ("/api/v1/internal",)


def _template_regex(path: str) -> "re.Pattern":
    return re.compile("^" + re.sub(r"\\\{[^/]+?\\\}", "([^/]+)", re.escape(path)) + "$")


_EXEMPT_PATTERNS = tuple((method, _template_regex(path)) for method, path in EXEMPT)


def normalize_origin(value: Optional[str]) -> Optional[str]:
    """``https://App.AlgoLab.me:443/`` -> ``https://app.algolab.me``; None when the value is not a plain http(s) origin (a path, a query, userinfo, ``null``)."""
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or parts.username is not None or parts.query or parts.fragment or parts.path not in ("", "/"):
        return None
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" + (f":{port}" if port not in (None, default) else "")


def referer_origin(value: Optional[str]) -> Optional[str]:
    """The origin a Referer (a full address, path included) points at."""
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    return normalize_origin(f"{parts.scheme}://{parts.netloc}") if parts.scheme and parts.netloc else None


def frontend_origin() -> Optional[str]:
    """The origin of ``FRONTEND_URL`` (its path, if somebody wrote one, is dropped: ``https://app.algolab.me/login`` -> ``https://app.algolab.me``)."""
    return referer_origin(os.getenv("FRONTEND_URL"))


def cabinet_origins() -> Optional[frozenset]:
    """The origins of the cabinets (CORS_ORIGINS + FRONTEND_URL, normalised); None = anybody (``*`` in CORS_ORIGINS)."""
    listed = list(get_settings().cors_origins)
    if "*" in listed:
        return None
    front = frontend_origin()
    allowed = {normalize_origin(o) for o in listed}
    if front:
        allowed.add(front)
    allowed.discard(None)
    return frozenset(allowed)


def cabinet_origin_allowed(origin: str) -> bool:
    allowed = cabinet_origins()
    return allowed is None or normalize_origin(origin) in allowed


def request_is_foreign(headers: Mapping[str, str], origin_allowed: Callable[[str], bool]) -> bool:
    """True when the headers say that a page of another site started the request (the rules in the module text). ``headers`` has lower-case names."""
    if (headers.get("sec-fetch-site") or "").strip().lower() == "cross-site":
        return True
    origin = headers.get("origin")
    if origin is not None:
        return not origin_allowed(origin)
    referer = headers.get("referer")
    if referer:
        found = referer_origin(referer)
        return found is None or not origin_allowed(found)
    return False


# Literal segments of the STAFF routes that a ``{name}`` of an exempt template must never stand for: ``POST /exams/by-code/print-package/start`` fits ``by-code/{code}/start``
# (code = print-package) AND the staff route ``/exams/{exam_id}/print-package/start`` (exam_id = by-code); the router picks the second, so the exemption must not take it.
RESERVED_PARAM_VALUES = frozenset({"by-code", "print-package"})


def is_exempt(method: str, path: str) -> bool:
    for m, pattern in _EXEMPT_PATTERNS:
        found = pattern.match(path) if m == method else None
        if found and not RESERVED_PARAM_VALUES.intersection(found.groups()):
            return True
    return False


STRICT_ENVS = frozenset({"production", "prod", "staging", "stage", "preview"})


def check_origin_settings(environ: Optional[Mapping[str, str]] = None) -> None:
    """The settings the source check stands on, looked at when the service starts (the lifespan).

    * ``*`` in ``CORS_ORIGINS`` on production/staging stops the start: with it the Origin of a request is never refused and the check shrinks to ``Sec-Fetch-Site`` alone.
      ``APP_ENV`` unset is read as production, as the settings read it. Without an ``environ`` argument (the real start) the values come from ``get_settings()``, as the service reads them.
    * ``FRONTEND_URL`` with a path or a query is a warning (any environment): only its ``scheme://host`` is the allowed origin; the rest is a mistake.
    """
    if environ is None:
        # What the service REALLY reads: the settings (any case of the name, the ``.env`` file), not ``os.environ`` (a lower-case name or a ``.env`` would slip past it).
        settings = get_settings()
        app_env, listed, env = settings.app_env.strip().lower(), [str(item) for item in settings.cors_origins], os.environ
    else:
        env = environ
        app_env = (env.get("APP_ENV") or "production").strip().lower()
        raw = (env.get("CORS_ORIGINS") or "").strip()
        try:
            parsed = json.loads(raw) if raw else []
            listed = [str(item) for item in parsed] if isinstance(parsed, list) else [str(parsed)]
        except ValueError:  # the settings refuse such a line themselves; a comma-separated one is judged here so that a mistake is not hidden
            listed = [item.strip() for item in raw.split(",") if item.strip()]
    if app_env in STRICT_ENVS and "*" in [item.strip() for item in listed]:
        raise RuntimeError(
            f"CRITICAL: CORS_ORIGINS содержит * при APP_ENV={app_env}: проверка источника изменяющих запросов (AD-2b) тогда не отказывает никому. "
            f"Перечислите адреса продукта явно."
        )
    front = (env.get("FRONTEND_URL") or "").strip()
    if front:
        try:
            parts = urlsplit(front)
            extra = parts.path not in ("", "/") or bool(parts.query) or bool(parts.fragment)
        except ValueError:
            extra = True
        if extra:
            logger.warning(
                "FRONTEND_URL содержит путь или параметры (%s): для проверки источника запросов берётся только адрес сайта (схема и хост); уберите лишнее.", front
            )


class OriginGuardMiddleware:
    """403 ``{"detail": text, "code": "bad_origin"}`` for a POST/PUT/PATCH/DELETE that another site started, before anything else runs (no cookie is read, no body is
    read, no route is matched). Everything else - GET, preflight, the exempt routes - passes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") in MUTATING_METHODS:
            path = scope.get("path", "")
            if not is_exempt(scope["method"], path):
                headers: dict = {}
                for name, value in scope.get("headers", []):
                    headers.setdefault(name.decode("latin-1").lower(), value.decode("latin-1"))  # the FIRST of a repeated header: the one Starlette's CORS reads too
                if request_is_foreign(headers, cabinet_origin_allowed):
                    logger.warning("foreign origin refused: %s %s origin=%r", scope.get("method"), path, headers.get("origin"))
                    await JSONResponse({"detail": BAD_ORIGIN_TEXT, "code": BAD_ORIGIN_CODE}, status_code=403)(scope, receive, send)
                    return
        await self.app(scope, receive, send)
