"""Wave A / A4 (exam): public surfaces.

EX-35 CORS for any localhost port only in development; EX-36 the textbook catalogue is staff only (and the student's
exam entry keeps working); EX-37 CSPRNG access codes + a brake on missed codes; EX-38 explicit public fields
(`has_group`, no group UUID, no pass-through student record); EX-59 minimal environment for helper processes, the
model key in a header, a validated request id.
"""
from __future__ import annotations

import re
import types

import pytest
import requests
from fastapi.testclient import TestClient

from src.api import router as router_module
from src.core import abuse_limit
from src.core import auth as core_auth
from src.core.config import get_settings
from src.domain import gemini_client, print_pdf
from src.core.middleware import sanitize_request_id
from tests.conftest import TEST_GROUP_ID, make_exam_in_db

API = "/api/v1"


# ── EX-35: CORS ──────────────────────────────────────────────────────────────────────────────────────────────
def _app_for(monkeypatch, app_env):
    import src.main as main_module

    monkeypatch.setenv("APP_ENV", app_env)
    get_settings.cache_clear()
    monkeypatch.setattr(main_module, "settings", get_settings())
    try:
        return main_module.create_app()
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("app_env, allowed", [("production", False), ("staging", False), ("development", True)])
def test_ex35_any_localhost_port_is_allowed_only_in_development(monkeypatch, app_env, allowed):
    app = _app_for(monkeypatch, app_env)
    http = TestClient(app)  # without ``with``: no lifespan (it would run the Postgres migrations)
    r = http.get("/health/live", headers={"Origin": "http://localhost:5173"})
    assert (r.headers.get("access-control-allow-origin") == "http://localhost:5173") is allowed
    # the configured origins keep working everywhere; a foreign origin never does
    configured = http.get("/health/live", headers={"Origin": "http://localhost:3000"})
    assert configured.headers.get("access-control-allow-origin") == "http://localhost:3000"
    foreign = http.get("/health/live", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in foreign.headers


# ── EX-36: the textbook catalogue ────────────────────────────────────────────────────────────────────────────
CATALOGUE = [
    "/textbooks",
    "/textbooks/TB1",
    "/textbooks/TB1/toc",
    "/textbooks/TB1/toc/tree",
    "/textbooks/TB1/coverage",
    "/textbooks/TB1/tasks?limit=200&offset=0",
    "/textbooks/skills/tree",
    "/toc/7/skills",
]


class FakeRepo:
    """The content-service reader: empty answers (what matters here is who gets through)."""

    def __getattr__(self, name):
        if name == "get_by_id":
            return lambda *a, **k: None
        return lambda *a, **k: []


@pytest.fixture
def real_auth(db, monkeypatch):
    """The app with the REAL staff dependency: auth-service answers from the cookie the caller sends."""
    from src.core.database import get_db
    from src.main import app

    calls = []

    class FakeAuthClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            calls.append(headers)
            cookie = (headers or {}).get("cookie", "")
            role = cookie.split("role=")[-1] if "role=" in cookie else None
            if role is None:
                return types.SimpleNamespace(status_code=401, json=lambda: {})
            return types.SimpleNamespace(
                status_code=200,
                json=lambda: {"id": "USR_X", "default_org_id": "ORG_X", "org_role": role, "role": role},
            )

    def override_get_db():
        yield db

    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(core_auth.httpx, "AsyncClient", FakeAuthClient)
    monkeypatch.setattr(router_module, "TextbookRepository", FakeRepo)
    with TestClient(app) as http:
        yield http
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", CATALOGUE)
def test_ex36_the_catalogue_refuses_anonymous_and_students(real_auth, path):
    anonymous = real_auth.get(API + path)
    assert anonymous.status_code == 401, path
    student = real_auth.get(API + path, headers={"cookie": "algo_auth_token=t; role=student"})
    assert student.status_code == 403, path
    parent = real_auth.get(API + path, headers={"cookie": "algo_auth_token=t; role=parent"})
    assert parent.status_code == 403, path


@pytest.mark.parametrize("path", CATALOGUE)
@pytest.mark.parametrize("role", ["teacher", "admin", "owner"])
def test_ex36_the_catalogue_answers_staff(real_auth, path, role):
    r = real_auth.get(API + path, headers={"cookie": f"algo_auth_token=t; role={role}"})
    # empty catalogue: 200 with a list, or the route's own 404 for an unknown textbook - never 401/403
    assert r.status_code in (200, 404), (path, r.status_code, r.text)
    if r.status_code == 404:
        assert "Учебник не найден" in r.json()["detail"]
    else:
        assert r.json() == []


def test_ex36_the_students_exam_page_does_not_need_the_catalogue(client, db):
    """What /e/<code> calls: by-code, check-student, start - all public, none touches /textbooks."""
    exam = make_exam_in_db(db, status="active")
    exam.access_code = "EXPUBLIC1"
    db.commit()
    r = client.get(f"{API}/exams/by-code/EXPUBLIC1")
    assert r.status_code == 200
    src = open("src/api/router.py", encoding="utf-8").read()
    public_block = src[src.index('@router.get("/exams/by-code/{code}"'):src.index("def _norm_ident")]
    assert "TextbookRepository" not in public_block


# ── EX-37: access codes ──────────────────────────────────────────────────────────────────────────────────────
def test_ex37_access_codes_come_from_the_csprng(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("random (Mersenne Twister) must not make access codes")

    monkeypatch.setattr(router_module.random, "choices", boom)
    monkeypatch.setattr(router_module.random, "choice", boom)
    codes = {router_module._generate_access_code() for _ in range(200)}
    assert len(codes) == 200
    assert all(re.fullmatch(r"EX[A-Z0-9]{8}", c) for c in codes)


def test_ex37_distinct_missed_codes_are_braked_per_address_and_real_codes_are_not(client, db):
    exam = make_exam_in_db(db, status="active")
    exam.access_code = "EXPUBLIC1"
    db.commit()
    mine = {"X-Real-IP": "203.0.113.7"}
    assert abuse_limit.CODE_MISSES_PER_MINUTE.count == 60 and abuse_limit.CODE_MISSES_PER_HOUR.count == 300
    for i in range(abuse_limit.CODE_MISSES_PER_MINUTE.count):
        assert client.get(f"{API}/exams/by-code/EXNOPE{i:04d}", headers=mine).status_code == 404
    refused = client.get(f"{API}/exams/by-code/EXNOPE9999", headers=mine)  # the 61st distinct wrong code in a minute
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1
    # the same address is refused for every public entry, even a real code (it is the address that walks)
    assert client.get(f"{API}/exams/by-code/EXPUBLIC1", headers=mine).status_code == 429
    # the others are not
    other = {"X-Real-IP": "203.0.113.8"}
    assert client.get(f"{API}/exams/by-code/EXPUBLIC1", headers=other).status_code == 200


def test_ex37_thirty_students_behind_one_nat_reloading_the_same_dead_code_are_never_braked(client, db):
    exam = make_exam_in_db(db, status="active")
    exam.access_code = "EXPUBLIC1"
    db.commit()
    nat = {"X-Real-IP": "198.51.100.30"}
    for _ in range(30):  # 30 students, each reloading the dead code from the board several times
        for _ in range(5):
            assert client.get(f"{API}/exams/by-code/EXDEAD0001", headers=nat).status_code == 404
    # ... and they can still open the real one
    assert client.get(f"{API}/exams/by-code/EXPUBLIC1", headers=nat).status_code == 200


def test_ex37_the_teachers_reset_lifts_the_miss_budget_of_the_addresses_that_opened_the_exam(client, db):
    exam = make_exam_in_db(db, status="active")
    exam.access_code = "EXPUBLIC1"
    db.commit()
    nat = {"X-Real-IP": "198.51.100.40"}
    stranger = {"X-Real-IP": "198.51.100.41"}  # never opened this exam
    assert client.get(f"{API}/exams/by-code/EXPUBLIC1", headers=nat).status_code == 200
    for who in (nat, stranger):
        for i in range(abuse_limit.CODE_MISSES_PER_MINUTE.count):
            assert client.get(f"{API}/exams/by-code/EXWALK{i:04d}", headers=who).status_code == 404
        assert client.get(f"{API}/exams/by-code/EXPUBLIC1", headers=who).status_code == 429
    reset = client.post(f"{API}/exams/{exam.id}/entry-limits/reset")
    assert reset.status_code == 200, reset.text
    assert client.get(f"{API}/exams/by-code/EXPUBLIC1", headers=nat).status_code == 200
    assert client.get(f"{API}/exams/by-code/EXPUBLIC1", headers=stranger).status_code == 429, "a walker gains nothing"


def test_ex37_a_class_behind_one_nat_opening_the_real_code_is_never_braked(client, db):
    exam = make_exam_in_db(db, status="active")
    exam.access_code = "EXPUBLIC1"
    db.commit()
    nat = {"X-Real-IP": "198.51.100.20"}
    for _ in range(150):
        assert client.get(f"{API}/exams/by-code/EXPUBLIC1", headers=nat).status_code == 200


def test_ex37_missed_codes_on_check_student_and_start_count_too(client):
    walker = {"X-Real-IP": "203.0.113.50"}
    limit = abuse_limit.CODE_MISSES_PER_MINUTE.count
    for i in range(limit):
        path = "check-student" if i % 2 else "start"
        r = client.post(f"{API}/exams/by-code/EXNOPE{i:04d}/{path}", json={"student_name": "Анна", "identifier": "TE-1"}, headers=walker)
        assert r.status_code == 404
    r = client.post(f"{API}/exams/by-code/EXNOPE9999/check-student", json={"identifier": "TE-1"}, headers=walker)
    assert r.status_code == 429


# ── EX-38: explicit public fields ────────────────────────────────────────────────────────────────────────────
def test_ex38_by_code_says_has_group_and_never_the_group_uuid(client, db):
    exam = make_exam_in_db(db, status="active")
    exam.access_code = "EXPUBLIC1"
    db.commit()
    body = client.get(f"{API}/exams/by-code/EXPUBLIC1").json()
    assert body["has_group"] is True
    assert "group_id" not in body
    assert TEST_GROUP_ID not in str(body)
    assert body["group_name"] == "8А"


def test_ex38_an_exam_without_a_group_says_has_group_false(client, db):
    exam = make_exam_in_db(db, status="active")
    exam.access_code = "EXPUBLIC1"
    exam.group_id = None
    exam.group_name = None
    exam.scheduled_lesson_id = None
    exam.scheduled_lesson_date = None
    db.commit()
    r = client.get(f"{API}/exams/by-code/EXPUBLIC1")
    if r.status_code == 200:  # an unbound online exam may still be refused by the binding rule: then there is no body
        assert r.json()["has_group"] is False
        assert "group_id" not in r.json()


def test_ex38_check_student_drops_whatever_else_the_roster_record_carries(client, db, monkeypatch):
    exam = make_exam_in_db(db, status="active")
    exam.access_code = "EXPUBLIC1"
    db.commit()

    async def roster_student(exam, *, student_id, student_name, identifier=None):
        return {
            "id": "STU_1", "display_id": "TE-001", "full_name": "Анна Каримова", "first_name": "Анна",
            "last_name": "Каримова", "class_level": 8,
            "phone": "+998901234567", "email": "a@example.com", "parent_phone": "+998909999999", "org_id": "ORG_TEST01",
            "notes": "private",
        }

    monkeypatch.setattr(router_module, "_canonical_student_for_exam", roster_student)
    r = client.post(f"{API}/exams/by-code/EXPUBLIC1/check-student", json={"identifier": "TE-001"})
    assert r.status_code == 200
    student = r.json()["student"]
    assert student == {
        "id": "STU_1", "first_name": "Анна", "last_name": "Каримова", "full_name": "Анна Каримова",
        "display_id": "TE-001", "class_level": 8,
    }
    for secret in ("+998", "example.com", "private", "ORG_TEST01"):
        assert secret not in r.text


# ── EX-59 ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_ex59_helper_processes_do_not_inherit_our_secrets(monkeypatch):
    for name, value in {
        "DATABASE_URL": "postgresql://u:pw@h/db", "GEMINI_API_KEY": "g-secret", "INTERNAL_SERVICE_TOKEN": "t-secret",
        "AZURE_DEEPSEEK_API_KEY": "a-secret", "REDIS_URL": "redis://h", "PATH": "/usr/bin:/bin", "HOME": "/home/x",
    }.items():
        monkeypatch.setenv(name, value)
    env = print_pdf._child_process_env()
    assert env["PATH"] == "/usr/bin:/bin" and env["HOME"] == "/home/x"
    assert not set(env) & {"DATABASE_URL", "GEMINI_API_KEY", "INTERNAL_SERVICE_TOKEN", "AZURE_DEEPSEEK_API_KEY", "REDIS_URL"}


def test_ex59_node_and_chromium_are_started_with_the_minimal_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "t-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "g-secret")
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd[0], kwargs.get("env")))
        raise RuntimeError("stop after the first process")

    monkeypatch.setattr(print_pdf.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(print_pdf.Path, "is_file", lambda self: True)
    monkeypatch.setattr(print_pdf.subprocess, "run", fake_run)
    print_pdf._render_katex_artifacts([("x^2", 12.0, "#000000")])
    assert seen and seen[0][0] == "/usr/bin/node"
    node_env = seen[0][1]
    assert node_env is not None and "KATEX_CSS_PATH" in node_env
    assert "INTERNAL_SERVICE_TOKEN" not in node_env and "GEMINI_API_KEY" not in node_env


def test_ex59_chromium_gets_an_explicit_environment_too(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "t-secret")
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((str(cmd[0]), kwargs.get("env")))
        if str(cmd[0]).endswith("node"):
            return types.SimpleNamespace(stdout="<html></html>")
        raise RuntimeError("stop after chromium started")

    monkeypatch.setattr(print_pdf.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(print_pdf.Path, "is_file", lambda self: True)
    monkeypatch.setattr(print_pdf.subprocess, "run", fake_run)
    print_pdf._render_katex_artifacts([("x^2", 12.0, "#000000")])
    chromium = [e for c, e in seen if "chromium" in c]
    assert chromium and chromium[0] is not None and "INTERNAL_SERVICE_TOKEN" not in chromium[0]


def test_ex59_the_gemini_key_travels_in_a_header_not_in_the_url(monkeypatch):
    monkeypatch.delenv("AZURE_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_DEEPSEEK_ENDPOINT", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g-secret-key")
    get_settings.cache_clear()
    sent = []

    def fake_post(url, params=None, json=None, headers=None, timeout=None):
        sent.append({"url": url, "params": params, "headers": headers})
        return types.SimpleNamespace(
            status_code=200, text="x", raise_for_status=lambda: None,
            json=lambda: {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
        )

    monkeypatch.setattr(requests, "post", fake_post)
    try:
        assert gemini_client.call_gemini("prompt", purpose="report_narrative") == "ok"
    finally:
        get_settings.cache_clear()
    assert sent[0]["params"] in (None, {})
    assert "key=" not in sent[0]["url"] and "g-secret-key" not in sent[0]["url"]
    assert sent[0]["headers"]["x-goog-api-key"] == "g-secret-key"


def test_ex59_request_id_is_taken_from_the_client_only_when_it_is_a_plain_token(client):
    for good in ["req-1234abcd-5678", "short", "a.b_c-1", "x" * 64]:
        assert client.get("/health/live", headers={"X-Request-ID": good}).headers["X-Request-ID"] == good
    for bad in ["x" * 65, "has space in it 1234", "a/b\\c:d;e,f 12345", "id-with-$(rm)-1234", "x=1&y=2;;;;"]:
        out = client.get("/health/live", headers={"X-Request-ID": bad}).headers["X-Request-ID"]
        assert out != bad
        assert re.fullmatch(r"[0-9a-f-]{36}", out), out
    assert re.fullmatch(r"[0-9a-f-]{36}", client.get("/health/live").headers["X-Request-ID"])


def test_ex59_sanitize_request_id_rejects_line_breaks_and_control_characters():
    for bad in ["abcdefgh\nINJECTED", "abcdefgh\r\n", "abcdefgh\x1b[31m", "\tabcdefgh", None, ""]:
        assert re.fullmatch(r"[0-9a-f-]{36}", sanitize_request_id(bad))
    assert sanitize_request_id("12345678") == "12345678"
    assert sanitize_request_id("a") == "a" and sanitize_request_id("a.b_c-d") == "a.b_c-d"
