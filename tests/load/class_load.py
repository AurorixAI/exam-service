"""A class writing one exam against the REAL deployment shape (ROADMAP #95).

Not a unit test: it starts

* an auth-service stand-in (``auth_stub.py``) that answers the two public student-entry calls with 50-200 ms latency,
* exam-service as ``uvicorn src.main:app --workers 2`` (the Dockerfile's command) on a real Postgres + Redis,

seeds one exam, and lets N concurrent "students" do by-code -> check-student -> start -> several progress saves
(some of them reload the page: a second ``start``) -> submit (every student double-clicks: two identical submits at
once) -> results.  A health prober polls ``/health/live`` and ``/health`` the whole time.  It reports latency per
endpoint, errors/timeouts and the final state of the database (every attempt submitted exactly once, no answer lost).
Exit code 1 on any failure, hang included.

    python -m tests.load.class_load --students 60 --pg postgresql://exam:exam@127.0.0.1:55437/exam_load \
        --redis redis://127.0.0.1:56391/0

Scenarios:  --mode active|lobby   the exam is running when the class arrives / the class waits in the lobby first
            --expire-after S      the exam's timer runs out S seconds into the run: the finalizer races the saves
            --teacher             a teacher polls lobby-status / live-summary / sessions the whole time
            --close-after S       ... and presses «close» S seconds after the exam started, while students submit
            --same-ip             the whole class behind ONE address (a school NAT)

A pytest wrapper (``tests/test_class_load_pg.py``) runs it when EXAM_LOAD_PG_URL / EXAM_LOAD_REDIS_URL are set.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
import sqlalchemy as sa

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
API = "/api/v1"
CODE = "LOAD2026"
EXAM_ID = "EXAM_LOAD01"
GROUP_ID = "11111111-2222-3333-4444-555555555555"
LESSON_ID = "LSN_LOAD01"
ORG_ID = "ORG_LOAD01"
TASKS = 10
_NAMESPACE = uuid.UUID("6f1c0c3e-0a52-4d57-9a3d-3b9d2f5a1e10")


@dataclass
class Sample:
    name: str
    status: str  # "200", "410", "timeout", "error:ReadError", ...
    seconds: float
    ok: bool = True  # an answer the scenario expects (200 normally; 400 for the second of two submits, ...)


@dataclass
class LoadReport:
    students: int
    mode: str
    workers: int
    expiry: bool = False  # the exam is closed under the students' hands (timer or teacher): finalize races the saves
    wall_seconds: float = 0.0
    hung: bool = False
    samples: list = field(default_factory=list)
    health: list = field(default_factory=list)  # (kind, ok, seconds)
    finished_students: int = 0
    student_errors: list = field(default_factory=list)
    db_problems: list = field(default_factory=list)
    server_log_tail: str = ""
    pg_activity: list = field(default_factory=list)  # who waits for / holds a lock when the run ended

    # -- numbers -------------------------------------------------------------------------------------------
    @staticmethod
    def _pct(values: list, q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]

    def by_endpoint(self) -> dict:
        out: dict = {}
        for name in sorted({s.name for s in self.samples}):
            rows = [s for s in self.samples if s.name == name]
            ok = [s.seconds for s in rows if s.ok]
            bad = [s for s in rows if not s.ok]
            out[name] = {
                "n": len(rows),
                "ok": len(ok),
                "p50": round(self._pct(ok, 0.5), 3),
                "p95": round(self._pct(ok, 0.95), 3),
                "max": round(max([s.seconds for s in rows] or [0]), 3),
                "bad": {k: sum(1 for s in bad if s.status == k) for k in sorted({s.status for s in bad})},
            }
        return out

    def overall_p95(self) -> float:
        return round(self._pct([s.seconds for s in self.samples if s.ok], 0.95), 3)

    def health_summary(self) -> dict:
        out: dict = {}
        for kind in sorted({h[0] for h in self.health}):
            rows = [h for h in self.health if h[0] == kind]
            out[kind] = {
                "n": len(rows),
                "failed": sum(1 for h in rows if not h[1]),
                "max_s": round(max(h[2] for h in rows), 3),
            }
        return out

    def failures(self) -> list:
        problems = list(self.db_problems)
        if self.hung:
            problems.append("HANG: the run did not finish before the deadline")
        bad = [s for s in self.samples if not s.ok]
        if bad:
            kinds = {f"{s.name}:{s.status}" for s in bad}
            problems.append(f"{len(bad)} unexpected answers: {sorted(kinds)[:8]}")
        if self.finished_students != self.students and not self.expiry:
            problems.append(f"only {self.finished_students}/{self.students} students finished")
        for kind, h in self.health_summary().items():
            if h["failed"]:
                problems.append(f"{kind} failed {h['failed']}x (max {h['max_s']}s)")
        problems += self.student_errors[:3]
        return problems

    def render(self) -> str:
        lines = [
            f"class load: {self.students} students, mode={self.mode}{'+expiry' if self.expiry else ''}, {self.workers}"
            f" workers, wall {self.wall_seconds:.1f}s, finished {self.finished_students}/{self.students},"
            f" overall p95 {self.overall_p95()}s"
        ]
        for name, row in self.by_endpoint().items():
            lines.append(
                f"  {name:<14} n={row['n']:<4} ok={row['ok']:<4} p50={row['p50']:<6} p95={row['p95']:<6} max={row['max']:<7}"
                f" bad={row['bad'] or '-'}"
            )
        lines.append(f"  health: {self.health_summary()}")
        for problem in self.failures():
            lines.append(f"  FAIL: {problem}")
        if self.failures() and self.pg_activity:
            lines.append("  pg_stat_activity (not idle):\n    " + "\n    ".join(self.pg_activity))
        if self.failures() and self.server_log_tail:
            lines.append("  server log tail:\n    " + self.server_log_tail.replace("\n", "\n    "))
        return "\n".join(lines)


# ── processes ────────────────────────────────────────────────────────────────────────────────────────────────
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _env(**extra: str) -> dict:
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")  # `alembic` of the same venv
    env.update(extra)
    return env


def _kill(proc: Optional[subprocess.Popen]) -> None:
    """Stop a child AND its workers (own process group): a hung uvicorn worker survives its killed master."""
    if proc is None:
        return

    def signal_group(sig: int) -> bool:
        try:
            os.killpg(proc.pid, sig)
            return True
        except (ProcessLookupError, PermissionError):  # the group is gone (macOS answers EPERM for a zombie leader)
            return False

    if signal_group(signal.SIGTERM):
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:  # a hung worker does not obey SIGTERM
            pass
    signal_group(signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


async def _wait_up(url: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get(url)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.3)
    raise RuntimeError(f"{url} did not come up in {seconds}s")


# ── data ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _tasks() -> list:
    return [
        {
            "task_id": f"TASK_{i:03d}",
            "skill_id": "SK_001",
            "skill_name": "Линейные уравнения",
            "difficulty": "B",
            "question_text": f"Решите уравнение: {i + 1}x = {(i + 1) * 3}",
            "question_latex": None,
            "question_image_url": None,
            "answer_type": "exact_number",
            "correct_answer": "3",
            "answer_options": None,
            "distractor_meta": None,
            "is_template_generated": False,
        }
        for i in range(TASKS)
    ]


def _drop_other_connections(pg_url: str) -> None:
    engine = sa.create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(sa.text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database()"
            " AND pid <> pg_backend_pid()"
        ))
    engine.dispose()


def _flush_redis(redis_url: str) -> None:
    import redis

    redis.from_url(redis_url).flushdb()  # a throwaway Redis of the load test, never a shared one


def _seed(pg_url: str, mode: str, expire_after: Optional[float] = None) -> None:
    engine = sa.create_engine(pg_url)
    today = datetime.now(ZoneInfo("Asia/Tashkent")).date().isoformat()
    status = "lobby" if mode == "lobby" else "active"
    minutes = 45
    settings: dict = {"time_minutes": 45, "mc_mode": "auto"}
    if status == "active":
        started = datetime.now(timezone.utc).replace(tzinfo=None)
        if expire_after is not None:  # a 1-minute exam that has `expire_after` seconds left
            minutes = 1
            settings["time_minutes"] = 1
            started -= timedelta(seconds=60 - expire_after)
        settings["started_at"] = started.isoformat()
    with engine.begin() as conn:
        conn.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
        conn.execute(sa.text("DELETE FROM exam_sessions WHERE exam_id = :e"), {"e": EXAM_ID})
        conn.execute(sa.text("DELETE FROM generated_exams WHERE id = :e"), {"e": EXAM_ID})
        conn.execute(
            sa.text(
                "INSERT INTO generated_exams (id, title, source_mode, variants, settings, skills_covered, toc_ids_used,"
                " org_id, access_code, status, delivery_mode, subject, teacher_name, total_time_minutes,"
                " tasks_per_variant, variants_count, group_id, group_name, class_level, scheduled_lesson_id,"
                " scheduled_lesson_date, created_by) VALUES (:id, 'Нагрузка', 'topics', CAST(:variants AS jsonb),"
                " CAST(:settings AS jsonb), ARRAY['SK_001'], ARRAY[]::integer[], :org, :code, :status, 'online',"
                " 'Математика', 'Учитель', :minutes, :n, 1, :group, '8А', 8, :lesson, :today, 'USR_LOAD')"
            ),
            {
                "id": EXAM_ID, "variants": json.dumps([{"variant_number": 1, "tasks": _tasks()}]),
                "settings": json.dumps(settings), "org": ORG_ID, "code": CODE, "status": status, "n": TASKS,
                "group": GROUP_ID, "lesson": LESSON_ID, "today": today, "minutes": minutes,
            },
        )
    engine.dispose()


def _start_the_lesson(pg_url: str) -> None:
    """What the teacher's «start» does to the row: lobby -> active with the common start time."""
    engine = sa.create_engine(pg_url)
    started = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE generated_exams SET status = 'active', settings = jsonb_set(settings, '{started_at}',"
                " to_jsonb(CAST(:ts AS text))) WHERE id = :e"
            ),
            {"ts": started, "e": EXAM_ID},
        )
    engine.dispose()


def _pg_activity(pg_url: str) -> list:
    """Sessions that wait for a lock or sit idle in a transaction: the signature of the self-deadlock."""
    engine = sa.create_engine(pg_url)
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT state, wait_event_type, COALESCE(EXTRACT(EPOCH FROM now() - xact_start), 0)::int AS xact_age,"
            " left(regexp_replace(query, '\\s+', ' ', 'g'), 90) AS q FROM pg_stat_activity"
            " WHERE datname = current_database() AND pid <> pg_backend_pid() AND state <> 'idle'"
        )).all()
    engine.dispose()
    return [f"{r.state} wait={r.wait_event_type} xact_age={r.xact_age}s {r.q}" for r in rows]


def _final_state(pg_url: str, ledger: dict, expiry: bool, expect_closed: bool = False) -> list:
    """Every attempt exists once, is submitted exactly once, and no acknowledged answer is missing."""
    engine = sa.create_engine(pg_url)
    problems: list = []
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT id, student_id, status, answers, submitted_at FROM exam_sessions WHERE exam_id = :e"),
            {"e": EXAM_ID},
        ).all()
        exam_status = conn.execute(sa.text("SELECT status FROM generated_exams WHERE id = :e"), {"e": EXAM_ID}).scalar()
    engine.dispose()
    if expect_closed and exam_status != "closed":
        problems.append(f"db: the teacher closed the exam but it is {exam_status}")
    by_student = {r.student_id: r for r in rows}
    if len(rows) != len(by_student):
        problems.append("db: several attempts for one student")
    started = {sid for sid, entry in ledger.items() if entry["session_id"]}
    if not expiry and len(rows) != len(ledger):
        problems.append(f"db: {len(rows)} attempts for {len(ledger)} students")
    for student_id, entry in ledger.items():
        row = by_student.get(student_id)
        if row is None:
            if student_id in started:
                problems.append(f"db: no attempt for {student_id}")
        elif row.status != "submitted":
            problems.append(f"db: attempt of {student_id} is {row.status}")
        elif entry["submitted"] is not None and dict(row.answers) != entry["submitted"]:
            problems.append(f"db: answers of {student_id} differ from what the 200 submit carried")
        elif any(dict(row.answers).get(k) != v for k, v in entry["acked"].items()):
            problems.append(f"db: an acknowledged save of {student_id} is missing")
        if len(problems) >= 6:
            break
    return problems


# ── students ─────────────────────────────────────────────────────────────────────────────────────────────────
class _Run:
    def __init__(self, cfg: argparse.Namespace) -> None:
        self.cfg = cfg
        self.samples: list = []
        self.student_errors: list = []
        self.finished = 0
        self.joined = 0
        self.ledger: dict = {}
        self.all_joined = asyncio.Event()
        self.started = asyncio.Event()  # the exam is running (set at once unless the class waits in the lobby)
        self.closed_sessions = None
        self.rng = random.Random(95)

    async def call(
        self, client: httpx.AsyncClient, name: str, method: str, path: str, *, accept: tuple = (200,), **kw: Any
    ) -> Optional[httpx.Response]:
        started = time.monotonic()
        try:
            response = await client.request(method, API + path, **kw)
            self.samples.append(
                Sample(name, str(response.status_code), time.monotonic() - started, response.status_code in accept)
            )
            return response
        except httpx.TimeoutException:
            self.samples.append(Sample(name, "timeout", time.monotonic() - started, False))
        except httpx.HTTPError as exc:
            self.samples.append(Sample(name, f"error:{type(exc).__name__}", time.monotonic() - started, False))
        return None

    async def student(self, index: int, client: httpx.AsyncClient) -> None:
        cfg = self.cfg
        # the exam may run out under the student's hands in the expiry scenario: «finished» answers are then fine
        closing = cfg.expire_after is not None or cfg.close_after is not None
        late = (200, 410) if closing else (200,)
        student_id = str(uuid.uuid5(_NAMESPACE, f"student-{index}"))
        name = f"Ученик {index:03d}"
        entry = self.ledger[student_id] = {"session_id": None, "acked": {}, "submitted": None}
        headers = {} if cfg.same_ip else {"X-Real-IP": f"10.{index // 250}.{index % 250}.{index % 7 + 1}"}
        await asyncio.sleep(self.rng.uniform(0, cfg.ramp))
        await self.call(client, "by-code", "GET", f"/exams/by-code/{CODE}", headers=headers, accept=late)
        r = await self.call(client, "check-student", "POST", f"/exams/by-code/{CODE}/check-student",
                            json={"student_id": student_id, "student_name": name}, headers=headers, accept=late)
        if r is None or r.status_code != 200:
            return
        start_body = {"student_name": name, "student_id": student_id, "class_level": 8}
        r = await self.call(client, "start", "POST", f"/exams/by-code/{CODE}/start", json=start_body, headers=headers,
                            accept=late)
        if r is None or r.status_code != 200:
            return
        started = r.json()
        if started.get("status") == "submitted":  # the timer ran out before the first task
            entry["session_id"] = started["session_id"]
            return
        session_id, token = started["session_id"], started["session_token"]
        entry["session_id"] = session_id
        auth = {"X-Exam-Session-Token": token}
        if started["status"] == "waiting":
            self.joined += 1
            if self.joined >= cfg.students:
                self.all_joined.set()
            while True:  # the lobby poll of the student page
                await asyncio.sleep(0.4 + self.rng.random() * 0.3)
                r = await self.call(client, "lobby-poll", "GET", f"/exams/by-code/{CODE}/session-status/{session_id}",
                                    headers=auth)
                if r is None or r.status_code != 200:
                    return
                if r.json().get("status") == "in_progress":
                    break
        answers: dict = {}
        reload_at = cfg.progress_saves // 2 if self.rng.random() < cfg.reload_ratio else -1
        for k in range(cfg.progress_saves):
            await asyncio.sleep(self.rng.uniform(0.15, 0.5))
            answers[f"TASK_{k:03d}"] = str(k)
            r = await self.call(client, "progress", "POST", f"/exam-sessions/{session_id}/progress",
                                json={"answers": {f"TASK_{k:03d}": str(k)}}, headers=auth, accept=late)
            if r is None or r.status_code != 200:
                break  # 410: the timer ran out, the finalizer submits what was saved
            if r.json().get("ok") is not True:
                return
            entry["acked"][f"TASK_{k:03d}"] = str(k)
            if k == reload_at:  # the student reloads the page: the same attempt must come back with the saved answers
                r = await self.call(client, "restart", "POST", f"/exams/by-code/{CODE}/start", json=start_body,
                                    headers=headers, accept=late)
                if r is None or r.status_code != 200:
                    break
                again = r.json()
                if again["session_id"] != session_id:
                    self.student_errors.append(f"{name}: a reload returned another attempt")
                    return
                if again.get("status") != "submitted" and again.get("saved_answers") != entry["acked"]:
                    self.student_errors.append(f"{name}: a reload lost answers")
                    return
                if await self.call(client, "status", "GET", f"/exams/by-code/{CODE}/session-status/{session_id}",
                                   headers=auth, accept=late) is None:
                    return
        answers["TASK_009"] = "3"
        body = {"answers": answers, "time_spent_seconds": 60}
        # a double click: the same submit twice at once.  Exactly one of them grades the attempt.
        accept = (200, 410) if closing else (200,)
        first, second = await asyncio.gather(
            self.call(client, "submit", "POST", f"/exam-sessions/{session_id}/submit", json=body, headers=auth,
                      accept=accept),
            self.call(client, "submit-dup", "POST", f"/exam-sessions/{session_id}/submit", json=body, headers=auth,
                      accept=accept),
        )
        codes = sorted(str(getattr(x, "status_code", None)) for x in (first, second))
        if not closing:
            if codes != ["200", "200"] or first.json() != second.json():
                self.student_errors.append(f"{name}: a double submit answered {codes} / different bodies, not twice the same 200")
                return
        elif "200" in codes and codes != ["200", "200"] and "410" not in codes:
            self.student_errors.append(f"{name}: a double submit answered {codes}")
            return
        if codes == ["200", "200"] and first.json() != second.json():
            self.student_errors.append(f"{name}: the two answers to a double submit differ")
            return
        winner = first if getattr(first, "status_code", None) == 200 else second
        # a 200 is also the STORED result of an attempt the teacher's close / the timer had graded a moment earlier
        # (it saved fewer answers than this submit carries: TASK_009 is never autosaved): only a submit that graded
        # the attempt itself pins the answers in the database
        if getattr(winner, "status_code", None) == 200 and winner.json()["answered_count"] == len(answers):
            entry["submitted"] = dict(answers)
        r = await self.call(client, "results", "GET", f"/exam-sessions/{session_id}/results", headers=auth)
        if r is None or r.status_code != 200:
            return
        self.finished += 1

    async def teacher(self, client: httpx.AsyncClient, stop: asyncio.Event, started: asyncio.Event) -> None:
        """A teacher watching the class: three panels polled all the time; «close» when asked, under the load."""
        headers = {"Authorization": "Bearer teacher-load"}

        async def poll(name: str, path: str, every: float) -> None:
            while not stop.is_set():
                await self.call(client, name, "GET", path, headers=headers)
                await asyncio.sleep(every)

        pollers = [
            asyncio.create_task(poll("t-lobby-status", f"/exams/{EXAM_ID}/lobby-status", 0.25)),
            asyncio.create_task(poll("t-live-summary", "/exams/live-summary", 0.5)),
            asyncio.create_task(poll("t-sessions", f"/exams/{EXAM_ID}/sessions", 0.7)),
        ]
        try:
            if self.cfg.close_after is not None:
                await started.wait()
                await asyncio.sleep(self.cfg.close_after)
                r = await self.call(client, "t-close", "POST", f"/exams/{EXAM_ID}/close", headers=headers)
                if r is not None and r.status_code == 200:
                    self.closed_sessions = r.json().get("finalized_sessions")
            await stop.wait()
        finally:
            for task in pollers:
                task.cancel()
            await asyncio.gather(*pollers, return_exceptions=True)

    async def lobby_flipper(self, pg_url: str) -> None:
        try:
            await asyncio.wait_for(self.all_joined.wait(), timeout=self.cfg.deadline / 2)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.3)
        await asyncio.to_thread(_start_the_lesson, pg_url)
        self.started.set()


async def _prober(base: str, stop: asyncio.Event, health: list) -> None:
    async with httpx.AsyncClient(timeout=3.0) as client:
        while not stop.is_set():
            for kind, path in (("live", "/health/live"), ("health", "/health")):
                started = time.monotonic()
                try:
                    ok = (await client.get(base + path)).status_code == 200
                except httpx.HTTPError:
                    ok = False
                health.append((kind, ok, time.monotonic() - started))
            await asyncio.sleep(0.5)


@dataclass
class Stack:
    base: str
    pg_url: str
    logfile: Any


@asynccontextmanager
async def running_stack(
    pg_url: str,
    redis_url: str,
    *,
    mode: str = "active",
    workers: int = 2,
    latency_ms: tuple = (50, 200),
    expire_after: Optional[float] = None,
    extra_env: Optional[dict] = None,
):
    """auth stub + exam-service (uvicorn, `workers` processes) + one seeded exam; everything is stopped on exit."""
    await asyncio.to_thread(_drop_other_connections, pg_url)  # a crashed earlier run must not leave its locks behind
    await asyncio.to_thread(_flush_redis, redis_url)  # the entry counters of an earlier run (same code, same IP)
    stub_port, app_port = _free_port(), _free_port()
    logfile = tempfile.NamedTemporaryFile("w+", suffix=".log", prefix="exam-load-", delete=False)
    stub = app = None
    try:
        stub = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "--app-dir", str(HERE), "auth_stub:app", "--port", str(stub_port),
             "--log-level", "warning"],
            env=_env(STUB_LAT_MIN_MS=str(latency_ms[0]), STUB_LAT_MAX_MS=str(latency_ms[1])),
            stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True,
        )
        env = _env(
            DATABASE_URL=pg_url, REDIS_URL=redis_url, AUTH_SERVICE_URL=f"http://127.0.0.1:{stub_port}",
            INTERNAL_SERVICE_TOKEN="load-test-token", APP_ENV="production", **(extra_env or {}),
        )
        app = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", str(app_port),
             "--workers", str(workers), "--log-level", "warning"],
            cwd=REPO, env=env, stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True,
        )
        base = f"http://127.0.0.1:{app_port}"
        await _wait_up(f"http://127.0.0.1:{stub_port}/openapi.json", 30)
        await _wait_up(base + "/health/live", 90)
        # the second worker may still be running its lifespan: wait until the schema exists, then seed
        for _ in range(60):
            try:
                await asyncio.to_thread(_seed, pg_url, mode, expire_after)
                break
            except sa.exc.ProgrammingError:
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError("schema did not appear")
        yield Stack(base, pg_url, logfile)
    finally:
        _kill(app)
        _kill(stub)
        logfile.flush()
        logfile.close()
        try:
            os.unlink(logfile.name)
        except OSError:
            pass


def _log_tail(logfile: Any) -> str:
    logfile.flush()
    logfile.seek(0)
    return "".join(line for line in logfile.readlines() if "HTTP Request" not in line)[-1800:]


async def run_load(
    students: int,
    pg_url: str,
    redis_url: str,
    *,
    mode: str = "active",
    workers: int = 2,
    latency_ms: tuple = (50, 200),
    progress_saves: int = 4,
    reload_ratio: float = 0.4,
    request_timeout: float = 20.0,
    deadline: float = 120.0,
    ramp: float = 3.0,
    same_ip: bool = False,
    expire_after: Optional[float] = None,
    teacher: bool = False,
    close_after: Optional[float] = None,
    extra_env: Optional[dict] = None,
) -> LoadReport:
    teacher = teacher or close_after is not None
    cfg = argparse.Namespace(
        students=students, progress_saves=progress_saves, reload_ratio=reload_ratio, ramp=ramp, same_ip=same_ip,
        deadline=deadline, expire_after=expire_after, close_after=close_after,
    )
    report = LoadReport(
        students=students, mode=mode, workers=workers, expiry=expire_after is not None or close_after is not None
    )
    async with running_stack(
        pg_url, redis_url, mode=mode, workers=workers, latency_ms=latency_ms, expire_after=expire_after,
        extra_env=extra_env,
    ) as stack:
        run = _Run(cfg)
        if mode != "lobby":
            run.started.set()
        stop_probe = asyncio.Event()
        stop_teacher = asyncio.Event()
        probe = asyncio.create_task(_prober(stack.base, stop_probe, report.health))
        limits = httpx.Limits(max_connections=students * 3 + 10)
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(base_url=stack.base, timeout=request_timeout, limits=limits) as client:
                tasks = [asyncio.create_task(run.student(i, client)) for i in range(students)]
                if mode == "lobby":
                    tasks.append(asyncio.create_task(run.lobby_flipper(pg_url)))
                teacher_task = asyncio.create_task(run.teacher(client, stop_teacher, run.started)) if teacher else None
                done, pending = await asyncio.wait(tasks, timeout=deadline)
                if pending:
                    report.hung = True
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if task.exception():
                        report.student_errors.append(repr(task.exception()))
                stop_teacher.set()
                if teacher_task is not None:
                    await asyncio.gather(teacher_task, return_exceptions=True)
            report.wall_seconds = time.monotonic() - started
            if report.hung:  # a few more probes while it is stuck, so the picture is complete
                await asyncio.sleep(3)
            report.pg_activity = await asyncio.to_thread(_pg_activity, pg_url)
        finally:
            stop_probe.set()
            await probe
        report.samples = run.samples
        report.student_errors += run.student_errors
        report.finished_students = run.finished
        if not report.hung:
            report.db_problems = await asyncio.to_thread(
                _final_state, pg_url, run.ledger, report.expiry, close_after is not None
            )
        report.server_log_tail = _log_tail(stack.logfile)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--students", type=int, default=60)
    parser.add_argument("--pg", default=os.getenv("EXAM_LOAD_PG_URL", "postgresql://exam:exam@127.0.0.1:55437/exam_load"))
    parser.add_argument("--redis", default=os.getenv("EXAM_LOAD_REDIS_URL", "redis://127.0.0.1:56391/0"))
    parser.add_argument("--mode", choices=("active", "lobby"), default="active")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--latency", default="50,200", help="auth-service stub latency range, ms")
    parser.add_argument("--saves", type=int, default=4)
    parser.add_argument("--reload-ratio", type=float, default=0.4)
    parser.add_argument("--timeout", type=float, default=20.0, help="per-request timeout, s")
    parser.add_argument("--deadline", type=float, default=90.0, help="whole-run deadline, s")
    parser.add_argument("--ramp", type=float, default=3.0, help="students arrive within this many seconds")
    parser.add_argument("--same-ip", action="store_true", help="every student behind one NAT address")
    parser.add_argument("--expire-after", type=float, default=None, help="the exam timer runs out this many seconds in")
    parser.add_argument("--teacher", action="store_true", help="a teacher polls the monitoring panels all the time")
    parser.add_argument("--close-after", type=float, default=None, help="the teacher closes the exam this many seconds after it started")
    parser.add_argument("--show-log", action="store_true", help="print the server log (without the per-request lines)")
    args = parser.parse_args()
    low, high = (int(x) for x in args.latency.split(","))
    report = asyncio.run(run_load(
        args.students, args.pg, args.redis, mode=args.mode, workers=args.workers, latency_ms=(low, high),
        progress_saves=args.saves, reload_ratio=args.reload_ratio, request_timeout=args.timeout,
        deadline=args.deadline, ramp=args.ramp, same_ip=args.same_ip, expire_after=args.expire_after,
        teacher=args.teacher, close_after=args.close_after,
    ))
    print(report.render())
    if args.show_log and not report.failures():
        print("server log (without request lines):\n" + (report.server_log_tail or "  <empty>"))
    return 1 if report.failures() else 0


if __name__ == "__main__":
    sys.exit(main())
