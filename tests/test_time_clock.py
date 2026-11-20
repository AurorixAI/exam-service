"""TIME-TESTS (P2): the exam service reads the controllable clock of the end-to-end stand (contract of auth-service ``business_clock``).

* the mode is decided once, at import: ``APP_ENV`` EXACTLY ``development`` and ``ALGO_TEST_CLOCK=1``; the switch on any other ``APP_ENV`` stops the start;
* outside the mode: offset 0, no Redis client, no Redis call, ``GET /api/v1/dev/clock`` is a 404, ``test_clock`` of the health answer is false;
* in the mode: ``_business_today()`` follows the shared offset (+1 day -> tomorrow, and "the lesson is today" changes with it), an unreadable offset is 503;
* the service never WRITES the offset.
The mode flag is a module constant, so the tests flip ``business_clock.TEST_CLOCK_MODE`` and give it a fake Redis with a call counter; the start refusal is
checked in a child process (the check runs at import)."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api import router as router_module
from src.core import business_clock as bc

ROOT = Path(__file__).resolve().parents[1]


class CountingRedis:
    def __init__(self, value="0", fail=None):
        self.value, self.fail, self.calls = value, fail, 0

    def get(self, key):
        self.calls += 1
        assert key == "algo:test:clock_offset_seconds"
        if self.fail:
            raise self.fail
        return self.value


@pytest.fixture
def stand(monkeypatch):
    """The stand mode with a fake Redis (the cache is reset, so a changed value is read at once)."""
    fake = CountingRedis()
    monkeypatch.setattr(bc, "TEST_CLOCK_MODE", True)
    monkeypatch.setattr(bc, "_redis_client", fake)
    monkeypatch.setattr(bc, "_offset_cache", (0, 0.0))
    return fake


def _import_in_child(env_extra: dict) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT), **env_extra}
    code = "import src.core.business_clock as bc; print('MODE', bc.TEST_CLOCK_MODE)"
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)


# ── the start ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("app_env", ["staging", "production", "testing", "Development", " development ", ""])
def test_the_switch_on_another_environment_stops_the_start(app_env):
    done = _import_in_child({"APP_ENV": app_env, "ALGO_TEST_CLOCK": "1"})
    assert done.returncode != 0
    assert "ALGO_TEST_CLOCK" in done.stderr and "APP_ENV=development" in done.stderr


def test_any_non_empty_value_stops_the_start_outside_development():
    done = _import_in_child({"APP_ENV": "staging", "ALGO_TEST_CLOCK": "0"})
    assert done.returncode != 0 and "ALGO_TEST_CLOCK" in done.stderr


def test_the_modes_of_the_start():
    cases = [
        ({"APP_ENV": "development", "ALGO_TEST_CLOCK": "1"}, "MODE True"),
        ({"APP_ENV": "development"}, "MODE False"),
        ({"APP_ENV": "development", "ALGO_TEST_CLOCK": "yes"}, "MODE False"),
        ({"APP_ENV": "staging"}, "MODE False"),
        ({"APP_ENV": "staging", "ALGO_TEST_CLOCK": ""}, "MODE False"),
    ]
    for env, expected in cases:
        done = _import_in_child(env)
        assert done.returncode == 0, (env, done.stderr)
        assert expected in done.stdout, (env, done.stdout)


def test_the_environment_name_is_compared_exactly():
    assert bc.is_dev_env({"APP_ENV": "development"}) and not bc.is_dev_env({"APP_ENV": " Development "})
    with pytest.raises(RuntimeError):
        bc.check_test_clock_allowed({"APP_ENV": " Development ", "ALGO_TEST_CLOCK": "1"})
    bc.check_test_clock_allowed({"APP_ENV": "development", "ALGO_TEST_CLOCK": "1"})
    bc.check_test_clock_allowed({"APP_ENV": "staging"})


# ── the mode is off ──────────────────────────────────────────────────────────
def test_off_mode_never_touches_redis(monkeypatch):
    fake = CountingRedis("86400")
    monkeypatch.setattr(bc, "_redis_client", fake)
    monkeypatch.setattr(bc, "_redis", lambda: pytest.fail("the Redis client was asked for outside the test mode"))
    monkeypatch.setattr(bc, "_offset_lock", None)  # even the lock is not taken: using it would raise
    assert bc.TEST_CLOCK_MODE is False
    for _ in range(3):
        assert bc.current_offset_seconds() == 0
        router_module._business_now()
        router_module._business_today()
    assert fake.calls == 0


def test_off_mode_has_no_clock_path_and_the_health_says_false(client):
    assert client.get("/api/v1/dev/clock").status_code == 404
    from fastapi.testclient import TestClient
    from src.main import app

    answer = TestClient(app).get("/health/ready")  # 200 or 503 by the dependencies; the field is there in both
    assert answer.json()["test_clock"] is False


def test_the_service_has_no_way_to_write_the_offset(client):
    for method in ("put", "post", "delete", "patch"):
        assert getattr(client, method)("/api/v1/dev/clock").status_code in (404, 405)
    source = (ROOT / "src" / "core" / "business_clock.py").read_text(encoding="utf-8")
    assert ".set(" not in source and ".delete(" not in source and ".incr" not in source


# ── the mode is on ───────────────────────────────────────────────────────────
def test_offset_shifts_business_today_to_tomorrow(stand):
    real_today = router_module._business_today()
    assert stand.calls == 1 and bc.current_offset_seconds() == 0
    stand.value = str(86400)
    bc._offset_cache = (0, 0.0)
    assert router_module._business_today() == real_today + timedelta(days=1)
    stand.value = "-86400"
    bc._offset_cache = (0, 0.0)
    assert router_module._business_today() == real_today - timedelta(days=1)


def test_offset_is_cached_for_a_fraction_of_a_second(stand):
    stand.value = "3600"
    assert bc.current_offset_seconds() == 3600
    assert bc.current_offset_seconds() == 3600
    assert stand.calls == 1


def test_the_decision_lesson_today_changes_with_the_offset(stand, monkeypatch):
    async def occurrence(*a, **k):
        return None

    async def status(*a, **k):
        return "in_progress"

    monkeypatch.setattr(router_module, "_resolve_lesson_occurrence", occurrence)
    monkeypatch.setattr(router_module, "_lesson_attendance_status", status)
    today = router_module._business_today().isoformat()
    exam = SimpleNamespace(group_id="g", scheduled_lesson_id="l", scheduled_lesson_date=today)

    asyncio.run(router_module._require_lesson_in_progress(exam, SimpleNamespace()))  # offset 0: the lesson is today, the exam may start
    stand.value = str(86400)
    bc._offset_cache = (0, 0.0)
    with pytest.raises(HTTPException) as refused:  # the stand moved a day on: the lesson of "today" is yesterday's
        asyncio.run(router_module._require_lesson_in_progress(exam, SimpleNamespace()))
    assert refused.value.status_code == 409 and "в день назначенного занятия" in refused.value.detail
    exam.scheduled_lesson_date = (router_module._business_today()).isoformat()
    asyncio.run(router_module._require_lesson_in_progress(exam, SimpleNamespace()))  # a lesson of the shifted today is fine


def test_clock_endpoint_shows_the_shared_clock(stand, client):
    stand.value = "7200"
    answer = client.get("/api/v1/dev/clock")
    assert answer.status_code == 200
    body = answer.json()
    assert body["offset_seconds"] == 7200 and set(body) == {"now_business", "now_utc", "offset_seconds"}
    assert body["now_business"].endswith("+05:00")


@pytest.mark.parametrize(
    "fake",
    [CountingRedis(fail=ConnectionError("down")), CountingRedis(value="soon"), CountingRedis(value="1.5")],
    ids=["redis-down", "not-a-number", "float"],
)
def test_an_unreadable_offset_is_loud(stand, monkeypatch, client, fake):
    monkeypatch.setattr(bc, "_redis_client", fake)
    answer = client.get("/api/v1/dev/clock")
    assert answer.status_code == 503
    with pytest.raises(bc.TestClockError):
        router_module._business_now()


def test_no_redis_address_is_loud(stand, monkeypatch, client):
    monkeypatch.setattr(bc, "_redis_client", None)
    monkeypatch.delenv("ALGO_TEST_CLOCK_REDIS_URL", raising=False)
    assert client.get("/api/v1/dev/clock").status_code == 503
    with pytest.raises(bc.TestClockError, match="ALGO_TEST_CLOCK_REDIS_URL"):
        bc.current_offset_seconds()


def test_empty_key_means_no_shift(stand):
    stand.value = None
    assert bc.current_offset_seconds() == 0
