"""K2-1 (m3): exam-service sends auth-service the ACCESS cookie and ``Authorization`` only. The refresh cookie (Path=/ since the ``__Host-`` rule, 14 days) and any other
cookie of the browser never leave this process. ``/auth/me`` is asked on every request, with no cache between requests."""
from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request

from src.core import auth as exam_auth


def make_request(cookie=None, authorization=None):
    headers = []
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request({"type": "http", "method": "GET", "path": "/api/v1/exams", "headers": headers, "query_string": b""})


SECURE = "theme=dark; __Host-algo_refresh_token=REFRESH.JWT.SECRET; __Host-algo_auth_token=ACCESS.JWT; x=1"
PLAIN = "algo_refresh_token=REFRESH.JWT.SECRET; algo_auth_token=ACCESS.JWT; _ga=GA1"


@pytest.mark.parametrize("cookie, expected", [
    (SECURE, "__Host-algo_auth_token=ACCESS.JWT"),
    (PLAIN, "algo_auth_token=ACCESS.JWT"),
    ("__Host-algo_auth_token=A; algo_auth_token=B", "__Host-algo_auth_token=A; algo_auth_token=B"),
])
def test_only_the_access_cookie_is_forwarded(cookie, expected):
    headers = exam_auth._forwarded_auth_headers(make_request(cookie=cookie))
    assert headers == {"cookie": expected}
    assert "REFRESH" not in str(headers) and "theme" not in str(headers)


def test_a_request_with_only_a_refresh_cookie_has_no_credential_at_all():
    assert exam_auth._forwarded_auth_headers(make_request(cookie="__Host-algo_refresh_token=R; algo_refresh_token=R")) == {}
    assert exam_auth._forwarded_auth_headers(make_request(cookie="")) == {}
    assert exam_auth._forwarded_auth_headers(make_request()) == {}


def test_authorization_is_forwarded_as_before_and_a_look_alike_cookie_name_is_not_taken():
    headers = exam_auth._forwarded_auth_headers(make_request(cookie="evil_algo_auth_token=X; algo_auth_token_x=Y", authorization="Bearer abc"))
    assert headers == {"authorization": "Bearer abc"}


def test_the_call_to_auth_me_carries_the_access_cookie_only_and_is_asked_again_on_every_request(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "u1", "default_org_id": "o1", "org_role": "teacher"}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            calls.append((url, dict(headers or {})))
            return FakeResponse()

    monkeypatch.setattr(exam_auth.httpx, "AsyncClient", FakeClient)
    for _ in range(2):                                                          # two requests: two calls (no cache between requests)
        actor = asyncio.run(exam_auth._resolve_staff_actor(make_request(cookie=SECURE)))
        assert actor.user_id == "u1" and dict(actor.auth_headers) == {"cookie": "__Host-algo_auth_token=ACCESS.JWT"}
    assert len(calls) == 2 and all(url.endswith("/api/v1/auth/me") for url, _ in calls)
    assert all("REFRESH" not in str(h) for _, h in calls)


def test_a_401_from_auth_is_a_401_here_at_once(monkeypatch):
    class Dead:
        status_code = 401

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None): return Dead()

    monkeypatch.setattr(exam_auth.httpx, "AsyncClient", FakeClient)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(exam_auth._resolve_staff_actor(make_request(cookie=SECURE)))
    assert exc.value.status_code == 401
