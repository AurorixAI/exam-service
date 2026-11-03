"""AD-2b: every mutating route of a STAFF member refuses a request that another site started; the student's routes and /internal do not judge it.

The guard test walks EVERY registered mutating route of the real application and puts it in exactly one of two boxes:

* STAFF: depends on ``require_exam_staff`` / ``require_heavy_staff`` (the staff cookie is read) -> must answer 403 ``bad_origin`` to a foreign Origin, to ``Sec-Fetch-Site:
  cross-site`` and to a foreign Referer;
* EXEMPT (``origin_guard.EXEMPT``, one by one, with the reason): the student's routes (by exam code / session capability token) and ``/internal`` -> must let such a request in
  (the live exam through an old address must not break).

A route that is in neither box fails ``test_every_mutating_route_is_staff_or_exempt_on_purpose`` with a question. No database is needed for the sweep: the guard answers before
anything is read.
"""
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.security.base import SecurityBase
from fastapi.testclient import TestClient

from src.core import auth as core_auth
from src.core import origin_guard
from src.core.config import get_settings
from src.main import app
from tests.conftest import make_exam_in_db, make_task

EVIL = "https://evil.example"
API = "/api/v1"
STAFF_DEPENDENCIES = {core_auth.require_exam_staff, core_auth.require_heavy_staff}

# The ONLY routes where a foreign Origin is not judged, one by one. Growing this set is a decision: write the reason in origin_guard.EXEMPT first.
EXPECTED_EXEMPT = {
    ("POST", "/api/v1/exams/by-code/{code}/check-student"),
    ("POST", "/api/v1/exams/by-code/{code}/start"),
    ("POST", "/api/v1/exam-sessions/{session_id}/progress"),
    ("POST", "/api/v1/exam-sessions/{session_id}/submit"),
    ("POST", "/api/v1/internal/lesson-bindings/cancellation-check"),
    ("POST", "/api/v1/internal/lesson-bindings/release"),
    ("POST", "/api/v1/internal/lesson-bindings/completion-check"),
    ("POST", "/api/v1/internal/lesson-bindings/completed"),
    ("POST", "/api/v1/internal/lesson-bindings/rescheduled"),
    ("POST", "/api/v1/internal/lesson-bindings/recurring-end-impact"),
    ("POST", "/api/v1/internal/lesson-bindings/recurring-end-release"),
    ("POST", "/api/v1/internal/group-bindings/archive-impact"),
    ("POST", "/api/v1/internal/group-bindings/archive-release"),
}


def _calls(dependant):
    """Every dependency callable under a route's dependant (recursively), and whether a cookie parameter is declared on the way."""
    found, direct = set(), bool(dependant.cookie_params)
    for sub in dependant.dependencies:
        if sub.call is not None:
            found.add(sub.call)
        inner, inner_direct = _calls(sub)
        found |= inner
        direct = direct or inner_direct
    return found, direct


def mutating_route_table():
    """{(METHOD, full path): (APIRoute, set of dependency callables, reads a cookie directly)} of every APIRoute of the application, found by walking the routes
    (and the included routers when FastAPI keeps them lazily), not by the OpenAPI document: hidden routes count."""
    found = {}

    def walk(routes, prefix, inherited):
        for route in routes:
            if hasattr(route, "original_router"):  # an included router (newer FastAPI includes lazily)
                context = route.include_context
                walk(route.original_router.routes, prefix + context.prefix, inherited | {d.dependency for d in context.dependencies})
            elif isinstance(route, APIRoute):
                calls, direct = _calls(route.dependant)
                calls = calls | inherited | {d.dependency for d in route.dependencies}
                for method in route.methods & origin_guard.MUTATING_METHODS:
                    found[(method, prefix + route.path)] = (route, calls, direct)

    walk(app.routes, "", set())
    return found


TABLE = mutating_route_table()
ROUTES = sorted(TABLE)
STAFF_ROUTES = [key for key in ROUTES if TABLE[key][1] & STAFF_DEPENDENCIES]
EXEMPT_ROUTES = [key for key in ROUTES if key in origin_guard.EXEMPT]


def concrete(path):
    return re.sub(r"\{[^}]+\}", "x", path)


def plain_client():
    return TestClient(app, raise_server_exceptions=False)


def own_origin():
    return get_settings().cors_origins[0]


def is_bad_origin(response):
    return (
        response.status_code == 403
        and response.headers.get("content-type", "").startswith("application/json")
        and response.json().get("code") == origin_guard.BAD_ORIGIN_CODE
    )


# ===================================================================================================== the walk and the two boxes


def test_the_walk_finds_every_route_the_openapi_document_lists():
    listed = {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        for method in ops
        if method.upper() in origin_guard.MUTATING_METHODS
    }
    assert listed <= set(ROUTES)
    assert len(ROUTES) >= 30, "the walk found too little: a change of FastAPI's routing internals must not silently empty this test"
    assert len(STAFF_ROUTES) >= 20
    assert ("POST", "/api/v1/exams/{exam_id}/close") in STAFF_ROUTES and ("DELETE", "/api/v1/exams/{exam_id}") in STAFF_ROUTES
    assert ("POST", "/api/v1/exams/{exam_id}/report") in STAFF_ROUTES  # the heavy-staff dependency counts too
    assert ("PUT", "/api/v1/exams/{exam_id}/print-layout") in STAFF_ROUTES


def test_the_exemption_list_is_exactly_the_expected_one_and_every_entry_is_a_real_route():
    assert set(origin_guard.EXEMPT) == EXPECTED_EXEMPT
    assert EXPECTED_EXEMPT <= set(ROUTES), "an exempt entry that matches no route is a stale exemption"
    for key, reason in origin_guard.EXEMPT.items():
        assert reason.strip(), key  # the reason is written down


def test_every_mutating_route_is_staff_or_exempt_on_purpose():
    """A new mutating route that is neither a staff route (it would depend on require_exam_staff / require_heavy_staff) nor in origin_guard.EXEMPT is a QUESTION: the
    middleware judges it (a foreign Origin is refused - the safe side), but who calls it?  A page of the student or a service -> list it in EXEMPT with the reason (and
    here); a staff member -> give it the staff dependency."""
    undecided = [key for key in ROUTES if key not in STAFF_ROUTES and key not in origin_guard.EXEMPT]
    assert not undecided, (
        f"decide for each: staff route (add Depends(require_exam_staff)) or exempt on purpose (origin_guard.EXEMPT + EXPECTED_EXEMPT with the reason): {undecided}"
    )


def test_no_route_is_both_staff_and_exempt():
    both = [key for key in origin_guard.EXEMPT if TABLE[key][1] & STAFF_DEPENDENCIES]
    assert not both, f"an exempt route reads the staff cookie - it must be judged: {both}"


def test_a_new_route_under_the_internal_area_must_be_listed_on_purpose():
    unlisted = [
        key
        for key in ROUTES
        if any(key[1] == area or key[1].startswith(area + "/") for area in origin_guard.EXEMPT_AREAS) and key not in origin_guard.EXEMPT
    ]
    assert not unlisted, f"decide: exempt (origin_guard.EXEMPT with the reason: token-protected, no cookie) or judged (move it out of the area): {unlisted}"


def test_an_exempt_route_reads_no_cookie_of_a_staff_member_and_an_internal_one_checks_the_service_token():
    import inspect

    for key in origin_guard.EXEMPT:
        route, calls, direct = TABLE[key]
        cabinet = sorted(f"{c.__module__}.{getattr(c, '__name__', type(c).__name__)}" for c in calls if getattr(c, "__module__", "") == "src.core.auth")
        credentials = [c for c in calls if isinstance(c, SecurityBase)]  # HTTPBearer & co: a credential read
        assert not cabinet and not credentials and not direct, (key, cabinet, credentials, direct)
        header_names = {h.alias for h in route.dependant.header_params}
        assert "authorization" not in {h.lower() for h in header_names}, key
        source = inspect.getsource(route.endpoint)
        if key[1].startswith("/api/v1/internal/"):
            assert "X-Internal-Service-Token" in header_names and "_require_internal_service_token(" in source, key
        else:
            assert "X-Exam-Session-Token" in header_names or "/by-code/" in key[1], key  # the capability token, or the entry by the exam code
            assert "request.cookies" not in source and "_resolve_staff_actor" not in source, key


def test_the_exempt_routes_are_matched_by_method_and_segments_only():
    assert origin_guard.is_exempt("POST", "/api/v1/exams/by-code/ABC123/start")
    assert not origin_guard.is_exempt("GET", "/api/v1/exams/by-code/ABC123/start")
    assert not origin_guard.is_exempt("DELETE", "/api/v1/exam-sessions/s1/submit")
    assert not origin_guard.is_exempt("POST", "/api/v1/exams/by-code/a/b/start")
    assert not origin_guard.is_exempt("POST", "/api/v1/exams/by-code/ABC123/something-new")
    assert not origin_guard.is_exempt("POST", "/api/v1/exams/E1/start-live")
    assert not origin_guard.is_exempt("POST", "/api/v1/exams/E1/close")
    assert not origin_guard.is_exempt("POST", "/api/v1/internal/something-new")
    assert origin_guard.is_exempt("POST", "/api/v1/internal/lesson-bindings/release")
    assert not origin_guard.is_exempt("POST", "/api/v1/internal/lesson-bindings/release/x")
    # no staff route is hidden behind an exempt template
    for method, path in STAFF_ROUTES:
        assert not origin_guard.is_exempt(method, concrete(path)), (method, path)


# ===================================================================================================== the sweep of every route


FOREIGN = [
    {"Origin": EVIL},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://localhost:3000.evil.example"},
    {"Referer": EVIL + "/page"},
    {"Origin": "http://localhost:3000", "Sec-Fetch-Site": "cross-site"},
]
FOREIGN_IDS = ["foreign-origin", "null-origin", "cross-site", "suffix-attack", "foreign-referer", "own-origin-but-cross-site"]


@pytest.mark.parametrize("headers", FOREIGN, ids=FOREIGN_IDS)
def test_every_staff_route_refuses_a_request_that_another_site_started(headers):
    leaked = []
    for method, path in STAFF_ROUTES:
        response = plain_client().request(method, concrete(path), headers=headers)
        if not is_bad_origin(response):
            leaked.append((method, path, response.status_code))
    assert not leaked, f"staff routes that let another site through: {leaked}"


@pytest.mark.parametrize("headers", FOREIGN, ids=FOREIGN_IDS)
def test_every_mutating_route_outside_the_exemption_list_refuses_it_and_the_listed_ones_do_not_judge_it(headers):
    allowed, refused_wrongly, leaked = 0, [], []
    for method, path in ROUTES:
        response = plain_client().request(method, concrete(path), headers=headers)
        if (method, path) in origin_guard.EXEMPT:
            allowed += 1
            if is_bad_origin(response):
                refused_wrongly.append((method, path))
        elif not is_bad_origin(response):
            leaked.append((method, path, response.status_code))
    assert not leaked, f"routes that let another site through: {leaked}"
    assert not refused_wrongly, f"exempt routes that judged the origin: {refused_wrongly}"
    assert allowed == len(EXPECTED_EXEMPT)


def test_the_answer_is_403_with_a_readable_text_and_a_request_id():
    response = plain_client().post(f"{API}/exams/E1/close", headers={"Origin": EVIL})
    assert response.status_code == 403
    assert response.json() == {"detail": origin_guard.BAD_ORIGIN_TEXT, "code": "bad_origin"}
    assert response.headers.get("x-request-id")
    assert "set-cookie" not in response.headers and "access-control-allow-origin" not in response.headers


# ===================================================================================================== what must keep working


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl / server to server / the Node side of the front: neither Origin nor Referer nor Sec-Fetch-Site
        {"Origin": "OWN"},
        {"Origin": "OWN/"},
        {"Origin": "FRONT"},
        {"Origin": "OWN", "Sec-Fetch-Site": "same-origin"},
        {"Origin": "OWN", "Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "same-origin"},
        {"Referer": "OWN/teacher/exams?x=1"},
        {"Origin": "OWN", "Referer": EVIL},  # Origin decides when it is there
    ],
)
def test_our_own_pages_and_clients_without_headers_pass_the_guard(monkeypatch, headers):
    monkeypatch.setenv("FRONTEND_URL", "https://app.example.test")
    headers = {k: v.replace("OWN", own_origin()).replace("FRONT", "https://app.example.test") for k, v in headers.items()}
    response = plain_client().post(f"{API}/exams/E1/close", headers=headers)
    assert response.status_code == 401, (headers, response.text)  # no staff cookie: refused by the staff dependency, i.e. the guard let it in


def test_a_teacher_on_the_product_address_still_changes_things_and_a_foreign_page_does_not(client, db):
    """The whole chain on the real handler: the same close, three callers."""
    exam = make_exam_in_db(db, status="active")
    for headers in ({"Origin": EVIL}, {"Sec-Fetch-Site": "cross-site"}, {"Referer": EVIL + "/x"}):
        assert client.post(f"{API}/exams/{exam.id}/close", headers=headers).status_code == 403
    db.refresh(exam)
    assert exam.status == "active", "a foreign page closed somebody's live exam"
    assert client.post(f"{API}/exams/{exam.id}/close", headers={"Origin": own_origin(), "Sec-Fetch-Site": "same-origin"}).status_code == 200
    db.refresh(exam)
    assert exam.status == "closed"


@pytest.mark.parametrize("headers", FOREIGN, ids=FOREIGN_IDS)
def test_the_students_live_exam_works_through_an_old_or_foreign_address(client, db, headers):
    """The student's entry, autosave and submit pass whatever Origin the page has (the alias address, a bookmark, an in-app browser)."""
    exam = make_exam_in_db(db, status="active", tasks=[make_task(task_id="T1"), make_task(task_id="T2")])
    exam.access_code = "AD2B0001"
    db.commit()
    checked = client.post(f"{API}/exams/by-code/AD2B0001/check-student", json={"student_id": "STU-1", "student_name": "Асель"}, headers=headers)
    assert checked.status_code == 200 and checked.json()["enrolled"] is True, checked.text
    started = client.post(f"{API}/exams/by-code/AD2B0001/start", json={"student_name": "Асель", "student_id": "STU-1"}, headers=headers)
    assert started.status_code == 200, started.text
    session_id, token = started.json()["session_id"], started.json().get("session_token")
    auth = {"X-Exam-Session-Token": token} if token else {}
    saved = client.post(f"{API}/exam-sessions/{session_id}/progress", json={"answers": {"T1": "3"}}, headers={**auth, **headers})
    assert saved.status_code == 200, saved.text
    done = client.post(f"{API}/exam-sessions/{session_id}/submit", json={"answers": {"T1": "3", "T2": "3"}, "time_spent_seconds": 60}, headers={**auth, **headers})
    assert done.status_code == 200, done.text


def test_an_internal_call_with_a_foreign_origin_reaches_the_token_check_and_without_headers_too(client):
    body = {"org_id": "ORG_TEST01", "group_id": "g", "lesson_id": "l", "lesson_date": "2026-11-03"}
    for headers in ({}, {"Origin": EVIL}):
        response = client.post(f"{API}/internal/lesson-bindings/cancellation-check", json=body, headers=headers)
        assert not is_bad_origin(response) and response.status_code in (401, 403, 503), (headers, response.status_code, response.text)


def test_the_frontend_url_is_allowed_even_when_the_cors_list_does_not_name_it(monkeypatch):
    """LESSONS 68: the address the product is served from must not depend on a CORS list that is one step behind."""
    monkeypatch.setattr(get_settings(), "cors_origins", ["https://other.example.test"])
    monkeypatch.setenv("FRONTEND_URL", "https://app.example.test/")
    client = plain_client()
    close = f"{API}/exams/E1/close"
    assert client.post(close, headers={"Origin": "https://app.example.test"}).status_code == 401  # through the guard, stopped by the missing cookie
    assert client.post(close, headers={"Origin": "https://other.example.test"}).status_code == 401
    assert client.post(close, headers={"Origin": "https://app.example.test.evil.example"}).status_code == 403
    assert client.post(close, headers={"Origin": "http://app.example.test"}).status_code == 403  # another scheme is another origin


def test_without_a_frontend_url_the_cors_list_alone_decides(monkeypatch):
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    monkeypatch.setattr(get_settings(), "cors_origins", ["https://app.example.test"])
    client = plain_client()
    assert client.post(f"{API}/exams/E1/close", headers={"Origin": "https://app.example.test"}).status_code == 401
    assert client.post(f"{API}/exams/E1/close", headers={"Origin": "https://stage.example.test"}).status_code == 403


def test_a_wildcard_cors_list_lets_every_origin_in_but_never_a_cross_site_header(monkeypatch):
    monkeypatch.setattr(get_settings(), "cors_origins", ["*"])
    client = plain_client()
    assert client.post(f"{API}/exams/E1/close", headers={"Origin": EVIL}).status_code == 401
    assert client.post(f"{API}/exams/E1/close", headers={"Origin": EVIL, "Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_reads_and_preflights_are_not_judged_by_the_middleware():
    client = plain_client()
    assert client.get("/health/live", headers={"Origin": EVIL}).status_code == 200
    preflight = client.options(f"{API}/exams/E1/close", headers={"Origin": own_origin(), "Access-Control-Request-Method": "POST"})
    assert preflight.status_code == 200 and preflight.headers["access-control-allow-origin"] == own_origin()


def test_a_repeated_origin_header_is_judged_by_its_first_value_as_cors_reads_it():
    client = plain_client()
    close = f"{API}/exams/E1/close"
    assert client.post(close, headers=[("origin", EVIL), ("origin", own_origin())]).status_code == 403
    assert client.post(close, headers=[("origin", own_origin()), ("origin", EVIL)]).status_code == 401
    from starlette.requests import Request

    assert Request({"type": "http", "headers": [(b"origin", b"a"), (b"origin", b"b")]}).headers.get("origin") == "a"


# ===================================================================================================== start-up check


@pytest.mark.parametrize("env", ["production", "staging", "preview", "stage", "prod"])
@pytest.mark.parametrize("raw", ['["*"]', '["https://app.example.test", "*"]', "*", "https://app.example.test, *"])
def test_a_wildcard_in_cors_origins_stops_the_start_of_a_deployed_stack(env, raw):
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        origin_guard.check_origin_settings({"APP_ENV": env, "CORS_ORIGINS": raw})


def test_an_unset_app_env_is_read_as_production_like_the_settings_do():
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        origin_guard.check_origin_settings({"CORS_ORIGINS": '["*"]'})


@pytest.mark.parametrize(
    "environ",
    [
        {"APP_ENV": "production", "CORS_ORIGINS": '["https://app.algolab.me"]'},
        {"APP_ENV": "production"},
        {"APP_ENV": "staging", "CORS_ORIGINS": '["https://stage.algolab.me","https://app.algolab.me"]'},
        {"APP_ENV": "development", "CORS_ORIGINS": '["*"]'},
        {"APP_ENV": "testing", "CORS_ORIGINS": '["*"]'},
    ],
)
def test_explicit_origins_and_non_deployed_environments_start(environ):
    origin_guard.check_origin_settings(environ)


def test_the_lifespan_runs_the_start_check(monkeypatch):
    called = []
    monkeypatch.setattr("src.main.check_origin_settings", lambda: called.append(1))
    with TestClient(app):
        pass
    assert called == [1]


def test_a_frontend_url_with_a_path_still_names_its_site_and_is_reported_at_start(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(get_settings(), "cors_origins", ["https://other.example.test"])
    monkeypatch.setenv("FRONTEND_URL", "https://app.example.test/login?x=1")
    assert plain_client().post(f"{API}/exams/E1/close", headers={"Origin": "https://app.example.test"}).status_code == 401
    with caplog.at_level(logging.WARNING, logger="src.core.origin_guard"):
        origin_guard.check_origin_settings({"APP_ENV": "production", "FRONTEND_URL": "https://app.example.test/login"})
    assert any("FRONTEND_URL" in r.getMessage() and "https://app.example.test/login" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("front", ["https://app.example.test", "https://app.example.test/", ""])
def test_a_clean_frontend_url_is_not_reported(caplog, front):
    import logging

    with caplog.at_level(logging.WARNING, logger="src.core.origin_guard"):
        origin_guard.check_origin_settings({"APP_ENV": "production", "FRONTEND_URL": front, "CORS_ORIGINS": '["https://app.example.test"]'})
    assert not [r for r in caplog.records if "FRONTEND_URL" in r.getMessage()]


# ===================================================================================================== the pure decision (the same tables as auth-service AD-2)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://App.AlgoLab.me:443/", "https://app.algolab.me"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://localhost:80", "http://localhost"),
        ("https://[::1]:8443", "https://[::1]:8443"),
        ("null", None),
        ("", None),
        (None, None),
        ("https://user@app.algolab.me", None),
        ("https://app.algolab.me/path", None),
        ("https://app.algolab.me?x=1", None),
        ("ftp://app.algolab.me", None),
        ("app.algolab.me", None),
        ("https://app.algolab.me:notaport", None),
    ],
)
def test_normalize_origin(value, expected):
    assert origin_guard.normalize_origin(value) == expected


@pytest.mark.parametrize(
    "headers, foreign",
    [
        ({}, False),
        ({"origin": "https://ok.test"}, False),
        ({"origin": "https://evil.test"}, True),
        ({"origin": "https://ok.test", "sec-fetch-site": "cross-site"}, True),
        ({"origin": "https://ok.test", "sec-fetch-site": "same-site"}, False),
        ({"referer": "https://ok.test/a/b?c=d"}, False),
        ({"referer": "https://evil.test/a"}, True),
        ({"referer": "not a url"}, True),
        ({"origin": "https://ok.test", "referer": "https://evil.test/a"}, False),
        ({"origin": ""}, True),  # an empty Origin is not an origin
    ],
)
def test_request_is_foreign(headers, foreign):
    assert origin_guard.request_is_foreign(headers, lambda o: origin_guard.normalize_origin(o) == "https://ok.test") is foreign
