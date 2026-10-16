"""Authentication and authorisation for non-public exam endpoints.

The exam service intentionally does not decode JWTs itself: auth-service is
the single source of truth for token validity, organisation membership and
role.  This avoids keeping a second copy of the signing secret in every
service and makes token revocation / role changes take effect immediately.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import AsyncIterator, Mapping, Optional
from uuid import UUID

import httpx
from fastapi import HTTPException, Request, status

from src.core.config import get_settings


log = logging.getLogger("exam_service.auth")

_STAFF_ROLES = {"owner", "admin", "teacher"}


class StaffSlots:
    """A bounded number of staff requests in flight in this worker; a full house is refused at once, never queued.

    Teacher and admin routes wait on auth-service (hundreds of ms, seconds when it is loaded) holding a worker thread.
    Without a cap of their own, a burst of dashboard polls takes every thread of the pool and the students of a
    running exam queue behind them (ROADMAP #95 R3: students' p95 2.7 s under 80 polls).  Thread-safe and independent
    of any event loop; the cap is per worker process.
    """

    def __init__(self, size: int, write_reserve: int = 0) -> None:
        self._size = max(int(size), 1)
        # reads stop `write_reserve` slots earlier: the last ones are for requests that change something
        self._read_limit = max(self._size - max(int(write_reserve), 0), 1)
        self._used = 0
        self._lock = threading.Lock()

    def try_acquire(self, write: bool = True) -> bool:
        with self._lock:
            if self._used >= (self._size if write else self._read_limit):
                return False
            self._used += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._used = max(self._used - 1, 0)

    @property
    def in_use(self) -> int:
        return self._used

    @property
    def size(self) -> int:
        return self._size


_last_refusal_log = 0.0
_staff_slots: Optional[StaffSlots] = None
_staff_slots_lock = threading.Lock()


def staff_slots() -> StaffSlots:
    global _staff_slots
    if _staff_slots is None:
        with _staff_slots_lock:
            if _staff_slots is None:
                settings = get_settings()
                _staff_slots = StaffSlots(settings.teacher_slots, settings.teacher_write_reserve)
    return _staff_slots


def reset_staff_slots() -> None:
    """Tests: forget the limiter so the next request builds it from the current settings."""
    global _staff_slots
    _staff_slots = None


@dataclass(frozen=True)
class ExamActor:
    """Authenticated staff member, scoped to one organisation."""

    user_id: str
    org_id: str
    role: str
    auth_headers: Mapping[str, str]

    @property
    def is_org_admin(self) -> bool:
        return self.role in {"owner", "admin"}


def _forwarded_auth_headers(request: Request) -> dict[str, str]:
    """Forward only the credentials auth-service understands."""
    headers: dict[str, str] = {}
    if cookie := request.headers.get("cookie"):
        headers["cookie"] = cookie
    if authorization := request.headers.get("authorization"):
        headers["authorization"] = authorization
    return headers


async def require_exam_staff(request: Request) -> AsyncIterator[ExamActor]:
    """Take a staff slot (503 + Retry-After when the worker is full), resolve the actor, hold the slot until the end
    of the request.  A yield-dependency: FastAPI releases the slot when the response is done, whatever happened."""
    slots = staff_slots()
    if not slots.try_acquire(write=request.method not in {"GET", "HEAD", "OPTIONS"}):
        global _last_refusal_log
        if time.monotonic() - _last_refusal_log > 10:  # one line per ten seconds, not one per refused poll
            _last_refusal_log = time.monotonic()
            log.warning("staff slots exhausted (%d of %d in use): refusing %s", slots.in_use, slots.size, request.url.path)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Сервер сейчас обслуживает много запросов преподавателей. Подождите несколько секунд и повторите.",
            headers={"Retry-After": "2"},
        )
    try:
        yield await _resolve_staff_actor(request)
    finally:
        slots.release()


async def _resolve_staff_actor(request: Request) -> ExamActor:
    """Resolve a staff actor through auth-service and fail closed on errors."""
    cached = getattr(request.state, "exam_actor", None)
    if cached is not None:
        return cached

    headers = _forwarded_auth_headers(request)
    if not headers:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется аутентификация")

    auth_url = f"{get_settings().auth_service_url.rstrip('/')}/api/v1/auth/me"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(auth_url, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Сервис авторизации временно недоступен. Повторите попытку.",
        ) from exc

    if response.status_code in {401, 403}:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется действующая авторизация")
    if response.status_code != 200:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Не удалось подтвердить права доступа")

    data = response.json()
    role = data.get("org_role") or data.get("role")
    user_id = data.get("id")
    org_id = data.get("default_org_id")
    if not user_id or not org_id or role not in _STAFF_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав для работы с экзаменами")

    actor = ExamActor(
        user_id=str(user_id),
        org_id=str(org_id),
        role=str(role),
        auth_headers=headers,
    )
    request.state.exam_actor = actor
    return actor


async def require_group_access(actor: ExamActor, group_id: str) -> dict:
    """Return a group only when it belongs to the actor's organisation.

    A teacher may work only with their own group; owner and admin may work with
    every group in their organisation.  The group name and grade must always
    come from this response, never from a client request.
    """
    # ``groups.id`` is a UUID in auth-service.  Historical exam rows may
    # contain a pre-migration placeholder (for example ``GRP_test``).  Do not
    # call auth-service with such a value: its UUID route would return 500 and
    # one obsolete row would make the whole teacher library unavailable.
    # Treat it exactly as a missing group; the caller will simply hide that
    # inaccessible legacy exam from a teacher.
    try:
        UUID(str(group_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Группа не найдена") from exc

    auth_url = (
        f"{get_settings().auth_service_url.rstrip('/')}/api/v1/auth/org/groups/{group_id}"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(auth_url, headers=dict(actor.auth_headers))
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Не удалось проверить доступ к группе. Повторите попытку.",
        ) from exc

    if response.status_code == 404:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Группа не найдена")
    if response.status_code in {401, 403}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет доступа к этой группе")
    if response.status_code != 200:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Не удалось проверить группу")

    group = response.json()
    if actor.role == "teacher" and str(group.get("teacher_id") or "") != actor.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Учитель может работать только со своей группой")
    return group


async def require_student_history_access(actor: ExamActor, student_id: str) -> dict:
    """Verify current staff visibility of a student in auth-service.

    Exam attempts are stored in this service, but student ownership and the
    teacher's current group scope belong to auth-service. Reusing its student
    route prevents a direct history endpoint from becoming an organisation or
    former-group data leak.
    """
    try:
        UUID(str(student_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ученик не найден") from exc

    auth_url = (
        f"{get_settings().auth_service_url.rstrip('/')}/api/v1/auth/org/students/{student_id}"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(auth_url, headers=dict(actor.auth_headers))
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Не удалось проверить доступ к ученику. Повторите попытку.",
        ) from exc

    if response.status_code in {401, 403, 404}:
        # Do not disclose whether a student exists outside the actor's scope.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет доступа к ученику")
    if response.status_code != 200:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Не удалось проверить доступ к ученику",
        )
    return response.json()


def exam_access_group(exam: object, actor: ExamActor) -> Optional[str]:
    """Everything ``require_exam_access`` decides WITHOUT the network.

    Raises the refusal, or returns the id of the group whose access auth-service must still confirm (``None``: access
    is already granted).  Split out so a list of exams can be checked with one lookup per distinct group.

    Legacy rows without ``org_id`` are intentionally not exposed.  Showing
    them to an arbitrary staff member would reintroduce the data-leak this
    guard prevents; they must first be explicitly migrated to an organisation.
    """
    exam_org_id = getattr(exam, "org_id", None)
    if not exam_org_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Экзамен создан до внедрения изоляции организаций и требует привязки к организации",
        )
    if str(exam_org_id) != actor.org_id:
        # Deliberately indistinguishable from a missing exam.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Экзамен не найден")

    if actor.is_org_admin:
        return None

    group_id = getattr(exam, "group_id", None)
    if group_id:
        # A classroom run belongs to the current group scope. Its original
        # author loses access immediately when the group is reassigned.
        return str(group_id)
    # Unassigned library templates still belong to their author.
    if getattr(exam, "created_by", None) == actor.user_id:
        return None
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет доступа к этому экзамену")


async def require_exam_access(exam: object, actor: ExamActor) -> None:
    """Enforce organisation and teacher ownership for one exam."""
    group_id = exam_access_group(exam, actor)
    if group_id:
        await require_group_access(actor, group_id)
