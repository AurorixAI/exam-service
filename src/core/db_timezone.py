"""Database session time-zone contract (ROADMAP #49).

Every timestamp column is a naive ``timestamp without time zone``. A naive ``utcnow()`` is written as is, but
``func.now()``, ``server_default now()`` and aware datetimes are converted through the *session* TimeZone before
they land in such a column. The two agree only while every session runs in UTC, so the services pin it
instead of trusting the Postgres default or the container image:

* every engine passes ``-c timezone=UTC`` as a startup option (``utc_connect_args``);
* startup reads ``current_setting('TimeZone')`` and refuses to start when it is anything else
  (``verify_database_timezone``) - that also catches a ``PGTZ`` variable in the environment, which libpq sends
  after ``options`` and therefore overrides it.

SQLite (unit tests) takes neither: it has no session time zone and rejects Postgres options.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url

logger = logging.getLogger(__name__)

UTC_OPTION = "-c timezone=UTC"
_UTC_NAMES = frozenset({"utc", "etc/utc"})


class DatabaseTimezoneError(RuntimeError):
    """The database session is not in UTC; timestamps would be written shifted."""


def _is_postgres(url: Any) -> bool:
    try:
        return make_url(url).get_backend_name() == "postgresql"
    except Exception:  # not a URL we can read: leave it alone
        return False


def utc_connect_args(url: Any, connect_args: Optional[Mapping[str, Any]] = None) -> dict:
    """``connect_args`` with the UTC session option merged in for a Postgres URL; a copy otherwise.

    An ``options`` value already present (in ``connect_args`` or in the URL query) is kept and the UTC switch is
    appended, so it is the last ``-c timezone`` the server sees.
    """
    args = dict(connect_args or {})
    if not _is_postgres(url):
        return args
    existing = str(args.get("options") or make_url(url).query.get("options") or "").strip()
    if UTC_OPTION not in existing:
        existing = f"{existing} {UTC_OPTION}".strip()
    args["options"] = existing
    return args


def is_utc_timezone_name(name: Optional[str]) -> bool:
    return (name or "").strip().lower() in _UTC_NAMES


def check_session_timezone(name: Optional[str]) -> None:
    """Pure check of ``current_setting('TimeZone')``: UTC and Etc/UTC pass, anything else is CRITICAL and raises."""
    if is_utc_timezone_name(name):
        return
    message = (
        f"Database session TimeZone is {name!r}, expected 'UTC': naive timestamp columns would be written "
        "shifted. Refusing to start (ROADMAP #49)."
    )
    logger.critical(message)
    raise DatabaseTimezoneError(message)


def verify_database_timezone(engine: Engine) -> None:
    """Startup guard: read the TimeZone a fresh connection of ``engine`` really gets. One cheap query; skipped on SQLite."""
    if engine.dialect.name != "postgresql":
        return
    with engine.connect() as conn:
        name = conn.execute(text("SELECT current_setting('TimeZone')")).scalar()
    check_session_timezone(name)
