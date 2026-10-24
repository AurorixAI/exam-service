"""A3 (exam): the paid report model - emergency switch, per-exam cooldown, cache when nothing is new, deadline.

The model client is ALWAYS replaced: ``requests.post`` is a recorder in an autouse fixture (and ``call_gemini`` is
mocked at the report generator for the endpoint tests), so no test can reach the network or cost money.
"""
from __future__ import annotations

import json
import os
import logging
import types
from datetime import datetime, timedelta

import pytest
import requests

from src.core.config import get_settings
from src.domain import gemini_client, report_generator
from src.models import ExamReport, ExamSession
from tests.conftest import make_exam_in_db, make_session_in_db, make_task
from tests.test_staff_routes_safety import _graded_exam

API = "/api/v1"
SECRET_PROMPT = "PROMPT-WITH-STUDENT-ANSWERS-98765"
GOOD = json.dumps({"summary": "Класс справился", "strengths": [], "critical_gaps": []}, ensure_ascii=False)


# ── no test can reach the network ────────────────────────────────────────────

class Wire:
    def __init__(self):
        self.posts: list = []          # every request that WOULD have been paid for
        self.azure_status = 200
        self.azure_text = "azure answer"
        self.gemini_status = 200
        self.gemini_text = "gemini answer"


@pytest.fixture(autouse=True)
def wire(monkeypatch):
    w = Wire()

    def fake_post(url, params=None, json=None, headers=None, timeout=None):
        w.posts.append({"url": url, "timeout": timeout, "headers": headers, "params": params})
        if "azure.invalid" in url:
            return types.SimpleNamespace(
                status_code=w.azure_status, text="x",
                json=lambda: {"choices": [{"message": {"content": w.azure_text}}]},
                raise_for_status=lambda: None,
            )
        return types.SimpleNamespace(
            status_code=w.gemini_status, text="x",
            json=lambda: {"candidates": [{"content": {"parts": [{"text": w.gemini_text}]}}]},
            raise_for_status=lambda: None,
        )

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(gemini_client.time, "sleep", lambda s: None)
    yield w


@pytest.fixture(autouse=True)
def _clean_state():
    get_settings.cache_clear()
    report_generator._in_flight.clear()
    yield
    get_settings.cache_clear()
    report_generator._in_flight.clear()


def set_env(monkeypatch, *, flag=None, azure=False, gemini=False):
    for name in ("REPORT_NARRATIVE_ENABLED", "AZURE_DEEPSEEK_API_KEY", "AZURE_DEEPSEEK_ENDPOINT", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    if flag is not None:
        monkeypatch.setenv("REPORT_NARRATIVE_ENABLED", flag)
    if azure:
        monkeypatch.setenv("AZURE_DEEPSEEK_API_KEY", "k")
        monkeypatch.setenv("AZURE_DEEPSEEK_ENDPOINT", "https://azure.invalid/v1")
    if gemini:
        monkeypatch.setenv("GEMINI_API_KEY", "g")
    get_settings.cache_clear()


# ── the switch in the one place ──────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (None, True), ("true", True), ("1", True), ("", True), ("yes", True),
    ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
])
def test_switch_parsing_default_on(monkeypatch, value, expected):
    set_env(monkeypatch, flag=value)
    assert gemini_client.narrative_enabled() is expected


@pytest.mark.parametrize("flag,azure,gemini,posts", [
    (None, True, False, 1),
    ("true", False, True, 1),
    ("false", True, True, 0),     # off + keys present -> 0 paid requests
    ("0", True, False, 0),
    ("no", False, True, 0),
])
def test_report_call_flag_times_key(monkeypatch, wire, flag, azure, gemini, posts):
    set_env(monkeypatch, flag=flag, azure=azure, gemini=gemini)
    if posts:
        assert gemini_client.call_gemini(SECRET_PROMPT, purpose="report_narrative")
    else:
        with pytest.raises(gemini_client.NarrativeDisabledError):
            gemini_client.call_gemini(SECRET_PROMPT, purpose="report_narrative")
    assert len(wire.posts) == posts


def test_switch_on_without_any_key_makes_no_request(monkeypatch, wire):
    set_env(monkeypatch)
    with pytest.raises(RuntimeError):
        gemini_client.call_gemini(SECRET_PROMPT, purpose="report_narrative")
    assert wire.posts == []


@pytest.mark.parametrize("purpose", ["report_narrative", "task_templatize", "other"])
def test_the_switch_stops_every_paid_call_of_the_service(monkeypatch, wire, purpose):
    set_env(monkeypatch, flag="false", azure=True, gemini=True)
    with pytest.raises(gemini_client.NarrativeDisabledError):
        gemini_client.call_gemini(SECRET_PROMPT, purpose=purpose)
    assert wire.posts == []                                   # incl. the variant templating chain


def test_the_variant_templatizer_makes_no_request_while_the_switch_is_off(monkeypatch, wire):
    from src.domain import llm_templatizer

    set_env(monkeypatch, flag="false", azure=True, gemini=True)
    monkeypatch.setattr(gemini_client, "get_model", lambda *_: "m", raising=False)
    try:
        llm_templatizer.templatize_via_llm("Найдите x", "x+1=4", "3", "exact_number")
    except Exception:
        pass
    assert wire.posts == []


def test_the_gemini_key_goes_in_a_header_never_in_the_url(monkeypatch, wire):
    set_env(monkeypatch, gemini=True)
    assert gemini_client.call_gemini(SECRET_PROMPT, purpose="report_narrative") == "gemini answer"
    sent = wire.posts[0]
    assert "key=" not in sent["url"] and "?" not in sent["url"]
    assert sent["headers"]["x-goog-api-key"] == "g" and sent["params"] is None


def test_no_exception_or_log_line_carries_the_api_key(monkeypatch, caplog):
    secret = "AIzaSyTOP-SECRET-KEY-123456"
    monkeypatch.setenv("AZURE_DEEPSEEK_API_KEY", secret + "-azure")
    monkeypatch.setenv("AZURE_DEEPSEEK_ENDPOINT", "https://azure.invalid/v1")
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    monkeypatch.delenv("REPORT_NARRATIVE_ENABLED", raising=False)
    get_settings.cache_clear()

    def leaky_post(url, **kwargs):
        raise requests.ConnectionError(f"HTTPSConnectionPool: max retries exceeded with url {url} key {secret} {secret}-azure")

    monkeypatch.setattr(requests, "post", leaky_post)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError) as err:
            gemini_client.call_gemini(SECRET_PROMPT, purpose="report_narrative")
    assert secret not in str(err.value)
    assert secret not in "\n".join(r.getMessage() for r in caplog.records)


def test_a_non_connection_error_is_wrapped_without_the_key(monkeypatch, caplog):
    set_env(monkeypatch, gemini=True)

    def bad(url, **kwargs):
        raise ValueError("boom with key g inside")

    monkeypatch.setattr(requests, "post", bad)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError) as err:
            gemini_client.call_gemini(SECRET_PROMPT, purpose="report_narrative")
    assert "boom" in str(err.value) and "key g " not in str(err.value) and "***" in str(err.value)


def test_no_sleep_and_no_retrying_line_after_the_final_failed_attempt(monkeypatch, caplog):
    set_env(monkeypatch, gemini=True)
    slept = []
    monkeypatch.setattr(gemini_client.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(
        requests, "post",
        lambda url, **k: types.SimpleNamespace(status_code=503, text="x", json=lambda: {}, raise_for_status=lambda: None),
    )
    with caplog.at_level(logging.INFO, logger="gemini_client"):
        with pytest.raises(RuntimeError):
            gemini_client.call_gemini(SECRET_PROMPT, purpose="report_narrative", max_retries=3)
    assert slept == [1, 2]                                    # between the 3 attempts only, nothing after the last
    retrying = [r for r in caplog.records if "retrying" in r.getMessage()]
    assert len(retrying) == 2


def test_startup_warns_about_an_unrecognised_switch_value_and_stays_on(monkeypatch, caplog):
    set_env(monkeypatch, flag="flase")
    with caplog.at_level(logging.WARNING, logger="gemini_client"):
        assert gemini_client.warn_if_switch_unrecognised() is True
    assert any("unrecognised" in r.getMessage() and "flase" in r.getMessage() for r in caplog.records)
    assert gemini_client.narrative_enabled() is True          # still fail-open
    for ok in (None, "true", "false", "0", "no", ""):
        set_env(monkeypatch, flag=ok)
        assert gemini_client.warn_if_switch_unrecognised() is False


def test_every_paid_request_is_logged_without_prompt_or_answer(monkeypatch, wire, caplog):
    set_env(monkeypatch, azure=True, gemini=True)
    wire.azure_status = 500                                   # Azure fails -> Gemini is a second paid request
    with caplog.at_level(logging.INFO, logger="gemini_client"):
        gemini_client.call_gemini(SECRET_PROMPT, purpose="report_narrative")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("paid_llm_call ")]
    assert len(lines) == 2 and len(wire.posts) == 2
    assert "provider=azure" in lines[0] and "ok=false" in lines[0]
    assert "provider=gemini" in lines[1] and "ok=true" in lines[1]
    for line in lines:
        assert "service=exam" in line and "purpose=report_narrative" in line and "model=" in line and "duration_ms=" in line
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_PROMPT not in text and "gemini answer" not in text


# ── EX-25: one deadline for the whole chain ──────────────────────────────────

class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _slow_world(monkeypatch, per_request):
    clock = Clock()
    seen = []

    def post(url, params=None, json=None, headers=None, timeout=None):
        seen.append(timeout)
        assert isinstance(timeout, tuple) and len(timeout) == 2         # (connect, read)
        clock.now += min(per_request, timeout[1])          # a hanging server: the request lasts until its read timeout
        raise requests.ConnectionError("hung")

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(gemini_client, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    return clock, seen


def test_the_whole_chain_ends_within_100_seconds(monkeypatch):
    set_env(monkeypatch, azure=True, gemini=True)
    clock, seen = _slow_world(monkeypatch, per_request=1000)
    start = clock.now
    with pytest.raises(RuntimeError):
        gemini_client.call_gemini(SECRET_PROMPT, purpose="report_narrative")   # defaults: timeout 120, 3 retries
    assert clock.now - start <= 100.0 + 1e-6               # before: Azure 120 s + 3 x 120 s + 1+2+4 s of sleep
    assert all(c <= 5.0 and r <= 100.0 + 1e-6 for c, r in seen) and len(seen) <= 2


def test_gemini_retries_stop_when_the_deadline_is_near(monkeypatch):
    set_env(monkeypatch, gemini=True)
    clock, seen = _slow_world(monkeypatch, per_request=40)
    start = clock.now
    with pytest.raises(RuntimeError):
        gemini_client.call_gemini(SECRET_PROMPT, purpose="report_narrative", max_retries=10)
    assert clock.now - start <= 100.0 + 1e-6
    assert len(seen) < 10


def test_a_fast_chain_is_not_affected(monkeypatch, wire):
    set_env(monkeypatch, azure=True)
    assert gemini_client.call_gemini(SECRET_PROMPT, purpose="report_narrative") == "azure answer"
    connect, read = wire.posts[0]["timeout"]
    assert connect <= 5.0 and read <= 100.0


# ── EX-26: '//' inside a JSON string is content ──────────────────────────────

def test_parse_json_keeps_double_slash_inside_strings():
    raw = '{"summary": "см. https://example.org/x и/или//y", "n": 1}'
    assert gemini_client.parse_json_response(raw) == {"summary": "см. https://example.org/x и/или//y", "n": 1}
    assert gemini_client.parse_json_response("```json\n" + raw + "\n```")["summary"].startswith("см. https://")


def test_parse_json_still_tolerates_comments_outside_strings_and_latex():
    assert gemini_client.parse_json_response('{\n "a": "x//y", // c\n "b": 2 // d\n}') == {"a": "x//y", "b": 2}
    assert gemini_client.parse_json_response('{"a": "\\frac{1}{2}"}')["a"]
    assert gemini_client.parse_json_response("nonsense") == {} and gemini_client.parse_json_response("") == {}


# ── the endpoint: switch, EX-24, EX-40 ───────────────────────────────────────

class Calls(list):
    answer = GOOD


@pytest.fixture
def model(monkeypatch):
    calls = Calls()

    def fake(prompt, **kwargs):
        calls.append(kwargs.get("purpose"))
        return calls.answer

    monkeypatch.setattr(report_generator, "call_gemini", fake)
    return calls


def _post(client, exam, force=False):
    return client.post(f"{API}/exams/{exam.id}/report" + ("?force=true" if force else ""))


def _report(db, exam):
    db.expire_all()
    return db.query(ExamReport).filter(ExamReport.exam_id == exam.id).one()


def _age(db, exam, minutes):
    report = _report(db, exam)
    report.updated_at = datetime.utcnow() - timedelta(minutes=minutes)
    db.commit()


def _new_submission(db, exam, n=2):
    s = ExamSession(
        id=f"SES_NEW{n:02d}", exam_id=exam.id, student_name="Пётр Петров", variant_number=1,
        answers={"TASK_001": "3"}, score=10, max_score=10, correct_count=1, total_tasks=1,
        status="submitted", submitted_at=datetime.utcnow(), session_token=f"tok-{n}",
    )
    db.add(s)
    db.commit()
    return s


def test_switch_off_endpoint_makes_zero_calls_and_still_returns_the_report(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3SW001")
    set_env(monkeypatch, flag="false", azure=True, gemini=True)
    resp = _post(client, exam)
    assert resp.status_code == 200
    body = resp.json()
    assert model == []                                       # the model was not even asked
    assert body["narrative_generated"] is False              # EX-24: the flag tells the truth
    assert json.loads(body["narrative_ru"])["summary"]       # deterministic wording is there
    assert _report(db, exam).narrative_generated is False


def test_switch_on_calls_once_and_marks_narrative_generated(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3SW002")
    set_env(monkeypatch)
    resp = _post(client, exam)
    assert resp.status_code == 200 and resp.json()["narrative_generated"] is True
    assert model == ["report_narrative"]


def test_ex24_garbage_model_output_is_not_reported_as_ai(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3EX24")
    set_env(monkeypatch)
    model.answer = "I am not JSON"
    resp = _post(client, exam)
    assert resp.status_code == 200 and resp.json()["narrative_generated"] is False
    assert len(model) == 1


def test_ex24_model_error_is_not_reported_as_ai(client, db, monkeypatch):
    exam, _ = _graded_exam(db, "A3EX24B")
    set_env(monkeypatch)

    def boom(prompt, **kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(report_generator, "call_gemini", boom)
    resp = _post(client, exam)
    assert resp.status_code == 200 and resp.json()["narrative_generated"] is False


def test_second_force_within_ten_minutes_is_409_with_retry_after(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3CD001")
    set_env(monkeypatch)
    assert _post(client, exam).status_code == 200 and len(model) == 1
    first = _post(client, exam, force=True)
    assert first.status_code == 409
    body = first.json()
    assert body["code"] == "REPORT_COOLDOWN" and "мин" in body["detail"] and "Traceback" not in first.text
    retry = int(first.headers["retry-after"])
    assert 1 <= retry <= 600
    assert len(model) == 1                                   # the refusal cost nothing
    assert _post(client, exam).status_code == 200            # the cached report is still readable (no force)
    assert len(model) == 1


def test_force_after_the_cooldown_with_new_work_pays_once_more(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3CD002")
    set_env(monkeypatch)
    assert _post(client, exam).status_code == 200
    _age(db, exam, 11)
    _new_submission(db, exam)
    resp = _post(client, exam, force=True)
    assert resp.status_code == 200 and resp.json()["submitted_count"] == 2
    assert len(model) == 2
    assert _post(client, exam, force=True).status_code == 409    # and the window starts again
    assert len(model) == 2


def test_force_after_the_cooldown_without_new_work_returns_the_cache_and_zero_calls(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3CD003")
    set_env(monkeypatch)
    first = _post(client, exam)
    assert first.status_code == 200 and len(model) == 1
    _age(db, exam, 11)
    stamp = _report(db, exam).updated_at
    again = _post(client, exam, force=True)
    assert again.status_code == 200 and len(model) == 1      # no new graded work: 0 paid calls
    assert again.json()["narrative_ru"] == first.json()["narrative_ru"]
    assert _report(db, exam).updated_at == stamp              # the cache was not rewritten


def test_a_regrade_that_moves_the_class_numbers_counts_as_new_work(client, db, model, monkeypatch):
    exam, session = _graded_exam(db, "A3CD004")
    set_env(monkeypatch)
    assert _post(client, exam).status_code == 200
    report = _report(db, exam)
    report.avg_score_pct = 12.5                               # the stored numbers no longer match the sessions
    report.updated_at = datetime.utcnow() - timedelta(minutes=11)
    db.commit()
    assert _post(client, exam, force=True).status_code == 200
    assert len(model) == 2


def test_a_cached_report_without_ai_text_may_retry_the_model_after_the_cooldown(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3CD005")
    set_env(monkeypatch, flag="false")
    assert _post(client, exam).status_code == 200 and model == []     # deterministic, narrative_generated False
    set_env(monkeypatch)                                              # switch back on
    assert _post(client, exam, force=True).status_code == 409         # still inside the window
    _age(db, exam, 11)
    resp = _post(client, exam, force=True)
    assert resp.status_code == 200 and resp.json()["narrative_generated"] is True and len(model) == 1


def test_switch_off_force_after_cooldown_never_pays(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3CD006")
    set_env(monkeypatch, flag="false")
    assert _post(client, exam).status_code == 200
    _age(db, exam, 11)
    _new_submission(db, exam)
    resp = _post(client, exam, force=True)
    assert resp.status_code == 200 and resp.json()["submitted_count"] == 2 and model == []


def test_cache_path_still_marks_a_closed_exam_final(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3CD007")                         # the exam is already closed
    set_env(monkeypatch)
    assert _post(client, exam).status_code == 200
    report = _report(db, exam)
    report.is_final = False
    report.updated_at = datetime.utcnow() - timedelta(minutes=11)
    db.commit()
    resp = _post(client, exam, force=True)
    assert resp.status_code == 200 and resp.json()["is_final"] is True and len(model) == 1


def test_a_failed_forced_regeneration_keeps_the_previous_narrative_and_the_window(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3KEEP1")
    set_env(monkeypatch)
    first = _post(client, exam)
    assert first.status_code == 200 and first.json()["narrative_generated"] is True
    _age(db, exam, 11)
    stamp = _report(db, exam).updated_at
    _new_submission(db, exam)
    model.answer = "I am not JSON"
    resp = _post(client, exam, force=True)
    assert resp.status_code == 200 and len(model) == 2
    assert resp.json()["narrative_ru"] == first.json()["narrative_ru"]         # not overwritten with the fallback
    assert resp.json()["narrative_generated"] is True
    report = _report(db, exam)
    assert report.updated_at == stamp                                          # the failed attempt spent no window
    model.answer = GOOD
    retry = _post(client, exam, force=True)                                   # so the teacher can try again at once
    assert retry.status_code == 200 and len(model) == 3 and retry.json()["submitted_count"] == 2


def test_a_model_error_on_force_also_keeps_the_previous_narrative(client, db, monkeypatch):
    exam, _ = _graded_exam(db, "A3KEEP2")
    set_env(monkeypatch)
    monkeypatch.setattr(report_generator, "call_gemini", lambda *a, **k: GOOD)
    first = _post(client, exam)
    _age(db, exam, 11)
    stamp = _report(db, exam).updated_at
    _new_submission(db, exam)

    def boom(prompt, **kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(report_generator, "call_gemini", boom)
    resp = _post(client, exam, force=True)
    assert resp.status_code == 200 and resp.json()["narrative_ru"] == first.json()["narrative_ru"]
    assert _report(db, exam).updated_at == stamp


def test_a_failed_forced_retry_over_a_deterministic_report_saves_the_numbers_but_not_the_window(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3KEEP3")
    set_env(monkeypatch, flag="false")
    assert _post(client, exam).status_code == 200                              # deterministic report, no AI text
    set_env(monkeypatch)
    _age(db, exam, 11)
    stamp = _report(db, exam).updated_at
    _new_submission(db, exam)
    model.answer = "garbage"
    resp = _post(client, exam, force=True)
    assert resp.status_code == 200 and resp.json()["submitted_count"] == 2 and resp.json()["narrative_generated"] is False
    assert _report(db, exam).updated_at == stamp


def test_marking_a_cached_report_final_does_not_move_updated_at(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3FINAL")
    set_env(monkeypatch)
    assert _post(client, exam).status_code == 200
    report = _report(db, exam)
    report.is_final = False
    report.updated_at = datetime.utcnow() - timedelta(minutes=11)
    db.commit()
    stamp = _report(db, exam).updated_at
    resp = _post(client, exam, force=True)
    assert resp.status_code == 200 and resp.json()["is_final"] is True and len(model) == 1
    assert _report(db, exam).updated_at == stamp


def test_a_null_updated_at_falls_back_to_generated_at(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3NULL")
    set_env(monkeypatch)
    assert _post(client, exam).status_code == 200
    db.query(ExamReport).filter(ExamReport.exam_id == exam.id).update(
        {"updated_at": None, "generated_at": datetime.utcnow() - timedelta(minutes=11)})
    db.commit()
    _new_submission(db, exam)
    assert _post(client, exam, force=True).status_code == 200 and len(model) == 2
    db.query(ExamReport).filter(ExamReport.exam_id == exam.id).update(
        {"updated_at": None, "generated_at": datetime.utcnow()})
    db.commit()
    _new_submission(db, exam, n=3)
    assert _post(client, exam, force=True).status_code == 409                  # a fresh generated_at is still a window


def test_a_lost_race_between_two_workers_is_a_409_not_a_second_payment(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3RACE1")
    set_env(monkeypatch)
    assert _post(client, exam).status_code == 200
    _age(db, exam, 11)
    _new_submission(db, exam)
    real = report_generator._aggregate

    def aggregate_then_the_other_worker_wins(exam_, sessions_):
        out = real(exam_, sessions_)
        db.query(ExamReport).filter(ExamReport.exam_id == exam_.id).update({"updated_at": datetime.utcnow()})
        db.commit()                                               # the other worker claimed the window first
        return out

    monkeypatch.setattr(report_generator, "_aggregate", aggregate_then_the_other_worker_wins)
    resp = _post(client, exam, force=True)
    assert resp.status_code == 409 and resp.json()["code"] == "REPORT_COOLDOWN"
    assert len(model) == 1


def test_a_double_click_on_the_same_worker_is_refused_while_the_first_request_runs(client, db, model, monkeypatch):
    exam, _ = _graded_exam(db, "A3RACE2")
    set_env(monkeypatch)
    report_generator._in_flight.add(exam.id)                      # the first request is still waiting for the model
    resp = _post(client, exam)
    assert resp.status_code == 409 and resp.json()["code"] == "REPORT_IN_PROGRESS" and "Retry-After" in resp.headers
    assert model == []
    report_generator._in_flight.discard(exam.id)
    assert _post(client, exam).status_code == 200 and len(model) == 1
    assert exam.id not in report_generator._in_flight             # the slot is released afterwards


def test_the_slot_is_released_when_the_model_call_fails(client, db, monkeypatch):
    exam, _ = _graded_exam(db, "A3RACE3")
    set_env(monkeypatch)
    monkeypatch.setattr(report_generator, "call_gemini", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert _post(client, exam).status_code == 200
    assert report_generator._in_flight == set()


def test_no_submitted_works_is_still_422_and_costs_nothing(client, db, model, monkeypatch):
    set_env(monkeypatch)
    exam = make_exam_in_db(db, tasks=[make_task("TASK_001", answer="3")], status="closed")
    exam.access_code = "A3EMPTY"
    db.commit()
    make_session_in_db(db, exam, status="in_progress")
    resp = _post(client, exam)
    assert resp.status_code == 422 and model == []


# ── EX-40 on the real thing: two workers, one database ───────────────────────

PG_URL = os.getenv("EXAM_WA3_PG_URL")


@pytest.mark.skipif(not PG_URL, reason="set EXAM_WA3_PG_URL (a throwaway Postgres) to run")
def test_postgres_two_parallel_forces_pay_once(stub_auth_service, monkeypatch):
    """Both requests pass the cooldown pre-check; the atomic claim in the database lets exactly one of them pay."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    from datetime import date

    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.core.auth import require_exam_staff, require_heavy_staff
    from src.core.database import get_db
    from src.main import app
    from src.models.exam_models import GeneratedExam
    from src.models.shared import Base
    from tests.conftest import TEST_GROUP_ID, TEST_LESSON_ID, TEST_ORG_ID, TEST_USER_ID, make_actor

    engine = create_engine(PG_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def get_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = get_session
    app.dependency_overrides[require_exam_staff] = make_actor
    app.dependency_overrides[require_heavy_staff] = make_actor

    paid: list = []

    def slow_model(prompt, **kwargs):
        paid.append(1)
        time.sleep(0.5)
        return GOOD

    monkeypatch.setattr(report_generator, "call_gemini", slow_model)

    class NeverBlocks(set):                       # switch the in-process guard off: this test is about the database
        def add(self, item):
            pass

    monkeypatch.setattr(report_generator, "_in_flight", NeverBlocks())
    try:
        with factory() as db:
            db.add(GeneratedExam(
                id="EXAM_PG3", title="PG", variants=[{"variant_number": 1, "tasks": [make_task("TASK_001", answer="3")]}],
                settings={}, status="closed", org_id=TEST_ORG_ID, created_by=TEST_USER_ID, group_id=TEST_GROUP_ID,
                group_name="8А", class_level=8, scheduled_lesson_id=TEST_LESSON_ID,
                scheduled_lesson_date=date.today().isoformat(), access_code="PGCODE03", skills_covered=[], toc_ids_used=[],
            ))
            db.flush()
            db.add(ExamSession(
                id="SES_PG3A", exam_id="EXAM_PG3", student_name="Иван", variant_number=1, answers={"TASK_001": "3"},
                score=10, max_score=10, correct_count=1, total_tasks=1, status="submitted",
                submitted_at=datetime.utcnow(), session_token="pg3-a",
            ))
            db.commit()
        with TestClient(app, raise_server_exceptions=False) as c:
            assert c.post(f"{API}/exams/EXAM_PG3/report").status_code == 200
        assert len(paid) == 1
        with factory() as db:
            db.query(ExamReport).filter(ExamReport.exam_id == "EXAM_PG3").update(
                {"updated_at": datetime.utcnow() - timedelta(minutes=11)})
            db.add(ExamSession(
                id="SES_PG3B", exam_id="EXAM_PG3", student_name="Пётр", variant_number=1, answers={"TASK_001": "3"},
                score=10, max_score=10, correct_count=1, total_tasks=1, status="submitted",
                submitted_at=datetime.utcnow(), session_token="pg3-b",
            ))
            db.commit()

        def fire(_):
            with TestClient(app, raise_server_exceptions=False) as c:
                return c.post(f"{API}/exams/EXAM_PG3/report?force=true")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(fire, range(2)))
        assert sorted(r.status_code for r in results) == [200, 409]
        assert len(paid) == 2                      # one more payment, not two
        loser = next(r for r in results if r.status_code == 409)
        assert loser.json()["code"] == "REPORT_COOLDOWN" and int(loser.headers["retry-after"]) >= 1
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()
