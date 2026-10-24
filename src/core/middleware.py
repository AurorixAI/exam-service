"""
Exam Service — Shared middleware.

  • RequestIDMiddleware  — attaches / generates X-Request-ID per request
  • configure_logging()  — sets up structlog JSON logging (prod) or pretty (dev)
"""
from __future__ import annotations

import logging
import re
import sys
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


# ---------------------------------------------------------------------------
# Request-ID middleware
# ---------------------------------------------------------------------------

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def sanitize_request_id(value: str | None) -> str:
    """The caller's X-Request-ID when it is a plain token (letters, digits, dot, underscore, dash; 1-64 chars), else a fresh UUID.
    Whatever a client typed must never reach the log or the response header as it is (line breaks, JSON, ANSI)."""
    if value and _REQUEST_ID_RE.fullmatch(value):
        return value
    return str(uuid.uuid4())


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Propagate (a valid one) or generate X-Request-ID and bind it to structlog context."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = sanitize_request_id(request.headers.get("X-Request-ID"))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ---------------------------------------------------------------------------
# structlog configuration
# ---------------------------------------------------------------------------

def configure_logging(is_development: bool = False) -> None:
    """Call once at application startup."""
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if is_development:
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
