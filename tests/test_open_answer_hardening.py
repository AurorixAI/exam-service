"""Typed answers are student input: bounded, linear-time, never an exception, and one verdict everywhere.

A. an answer with > 4300 digits (ValueError in int()/Fraction() since Python 3.11), an oversized request, one bad
   answer among a whole class being finalized;
B. no quadratic regular expression on text the student typed;
C. a fraction cannot dodge the "lowest terms" rule with a thin space (`4/10\\,`);
D. the verdict and the score a student/teacher sees come from one grading.
"""
from __future__ import annotations

import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("APP_ENV", "testing")

import pytest

import src.domain.grading as grading
from src.api.router import _check_and_auto_close_exam
from src.domain import answer_utils as au
from src.domain import answer_value as av
from src.domain.answer_utils import MAX_OPEN_ANSWER_CHARS, check_open_answer
from src.domain.grading import GRADER_VERSION, GRADING_KEY, check_task_answer, stored_verdicts
from src.domain.report_generator import _aggregate
from tests.conftest import make_exam_in_db, make_session_in_db, make_task


def _task(answer="$\\dfrac{1}{2}$", task_id="T1", distractors=("$\\dfrac{1}{3}$", "$2$", "$\\dfrac{2}{3}$")):
    task = make_task(task_id, answer=answer)
    task["distractor_meta"] = [{"value": v, "error_type": "x", "explanation": "e"} for v in distractors]
    return task


def _exam(db, tasks, mc_mode="all_open", **settings):
    exam = make_exam_in_db(db, tasks=tasks)
    exam.settings = {"time_minutes": 45, "mc_mode": mc_mode, **settings}
    db.commit()
    return exam


# ── A. bounded input, no exceptions ───────────────────────────────────────────────────────────────────────────
HUGE = "9" * 5000      # > 4300 digits: int("9" * 5000) raises ValueError on Python 3.11


@pytest.mark.parametrize("typed", [HUGE, HUGE + "/3", "1 " + HUGE + "/3", "\\frac{" + HUGE + "}{3}", "0," + HUGE])
def test_a_number_with_thousands_of_digits_is_wrong_not_an_error(typed):
    assert check_open_answer(typed, "$\\dfrac{1}{2}$") is False
    assert check_open_answer(typed, "$" + HUGE + "$") is False       # over the length limit: never parsed


def test_a_parser_failure_inside_the_limit_is_wrong_not_an_error(monkeypatch):
    """The cap is the first guard; the except is the second (here the cap is lifted on purpose)."""
    monkeypatch.setattr(au, "MAX_OPEN_ANSWER_CHARS", 10**6)
    monkeypatch.setattr(av, "_MAX_NUMBER_CHARS", 10**6)
    monkeypatch.setattr(au, "_MAX_NUMBER_CHARS", 10**6)
    for typed in (HUGE, HUGE + "/3", "\\frac{" + HUGE + "}{3}"):
        assert check_open_answer(typed, "$\\dfrac{1}{2}$") is False
        assert check_open_answer(typed, "$" + HUGE[:-1] + "8$") is False


def test_the_length_limit_is_exact():
    at_limit = "1" * MAX_OPEN_ANSWER_CHARS
    assert check_open_answer(at_limit, "$" + at_limit + "$") is True
    assert check_open_answer(at_limit + "1", "$" + at_limit + "1$") is False
    assert check_open_answer(" " * 400 + "2", "$2$") is False         # padding is not an answer either


def test_exact_number_and_fraction_shape_do_not_raise_on_huge_numbers():
    assert av.exact_number(HUGE) is None
    assert av.exact_number("1/" + HUGE) is None
    assert au._fraction_shape("1/" + HUGE) is None


def test_grading_a_task_never_raises_and_does_not_log_the_answer(monkeypatch, caplog):
    def boom(*_args):
        raise ValueError("Invalid literal for Fraction: 'SECRET-ANSWER'")

    monkeypatch.setattr(grading, "check_open_answer", boom)
    with caplog.at_level(logging.WARNING, logger="src.domain.grading"):
        assert check_task_answer(_task(), "SECRET-ANSWER", "all_open") is False
    assert caplog.records and "T1" in caplog.text and "ValueError" in caplog.text
    assert "SECRET-ANSWER" not in caplog.text


def test_choice_grader_failure_is_wrong_not_an_error(monkeypatch):
    def boom(*_args):
        raise AttributeError("x")

    monkeypatch.setattr(grading, "check_answer", boom)
    assert check_task_answer(_task(), "$\\dfrac{1}{2}$", "auto") is False


@pytest.mark.parametrize("endpoint, method", [("submit", "post"), ("progress", "post")])
def test_oversized_answers_are_rejected_with_422(client, db, endpoint, method):
    exam = _exam(db, [_task()])
    session = make_session_in_db(db, exam)
    from tests.conftest import session_headers
    url = f"/api/v1/exam-sessions/{session.id}/{endpoint}"
    headers = session_headers(session.id)
    too_long = {"answers": {"T1": "x" * 1001}}                       # the documented limits: 1000 characters, 200 answers
    too_many = {"answers": {f"T{i}": "1" for i in range(201)}}
    long_id = {"answers": {"T" * 101: "1"}}
    assert getattr(client, method)(url, json=too_long, headers=headers).status_code == 422
    assert getattr(client, method)(url, json=too_many, headers=headers).status_code == 422
    assert getattr(client, method)(url, json=long_id, headers=headers).status_code == 422


def test_one_bad_answer_does_not_stop_the_close_of_the_exam(client, db, monkeypatch):
    tasks = [_task(task_id="T1"), _task(task_id="T2")]
    exam = _exam(db, tasks)
    good = make_session_in_db(db, exam)
    good.answers = {"T1": "1/2", "T2": "TRIGGER"}      # stored before the API cap existed or written by hand
    db.commit()
    real = au.check_open_answer

    def flaky(student, correct):
        if student == "TRIGGER":
            raise ValueError("boom")
        return real(student, correct)

    monkeypatch.setattr(grading, "check_open_answer", flaky)
    assert client.post(f"/api/v1/exams/{exam.id}/close").status_code == 200
    db.refresh(good)
    assert good.status == "submitted" and good.correct_count == 1 and good.score == 10


def test_one_bad_answer_does_not_stop_the_auto_close(db, monkeypatch):
    exam = _exam(db, [_task(task_id="T1")], started_at=(datetime.utcnow() - timedelta(hours=3)).isoformat())
    session = make_session_in_db(db, exam)
    session.answers = {"T1": "TRIGGER"}
    db.commit()
    monkeypatch.setattr(grading, "check_open_answer", lambda *_: (_ for _ in ()).throw(RecursionError()))
    assert _check_and_auto_close_exam(db, exam) is True
    db.refresh(session)
    assert session.status == "submitted" and session.correct_count == 0


# ── B. linear time ────────────────────────────────────────────────────────────────────────────────────────────
PATHOLOGICAL = {
    "dots": "." * 40000 + "x",
    "space-dot": " ." * 20000 + "x",
    "digits": "1" * 20000,
    "open-parens": "(" * 20000,
    "frac-open": "\\frac{" * 5000,
    "spaces": " " * 20000 + "x",
    "digit-spaces": "1" + " " * 20000 + "x",
    "sin-index": "\\sin" + "_a" * 10000,
    "brace-spaces": "^{" + " " * 20000,
    "digit-gaps": "1 " * 20000 + "x",
    "thin-spaces": "\\," * 20000 + "x",
    "mixed-spaces": "1" + " " * 20000 + "/" + "1" * 20000,
}
FUNCTIONS = {
    "exact_number": av.exact_number,
    "notation_key": av._notation_key,
    "answer_value_key": av.answer_value_key,
    "equivalent": lambda text: av.answers_equivalent(text, "$\\dfrac{1}{2}$"),
    "equivalent_reversed": lambda text: av.answers_equivalent("$\\dfrac{1}{2}$", text),
    "fraction_shape": au._fraction_shape,
    "is_empty_set": au._is_empty_set,
    "single_token_braces": lambda text: au._SINGLE_TOKEN_BRACES_RE.sub(r"\1\2", text),
    "open_match_without_cap": lambda text: au._open_answer_matches(text, "$\\dfrac{1}{2}$"),
    "check_open_answer": lambda text: check_open_answer(text, "$\\dfrac{1}{2}$"),
}


@pytest.mark.parametrize("name", sorted(PATHOLOGICAL))
def test_no_function_is_slower_than_50ms_on_hostile_text(name):
    """The functions are linear, so the same holds for a bank key (they are called with keys too)."""
    text = PATHOLOGICAL[name]
    for function_name, function in FUNCTIONS.items():
        started = time.perf_counter()
        function(text)                       # must not raise either
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert elapsed_ms < 50, f"{function_name} on {name}: {elapsed_ms:.0f} ms"


def test_trailing_punctuation_and_mixed_fraction_keep_their_meaning():
    assert av._notation_key("5. ;\n") == av._notation_key("5")
    assert av.exact_number("5 \\dfrac{8}{9}") == av.exact_number("53/9")
    assert av.exact_number("-\\dfrac{ 1 }{ 2 }") == av.exact_number("-1/2")
    assert av.exact_number("  \\dfrac{1}{2}") == av.exact_number("0,5")


# ── C. thin spaces do not hide an unreduced fraction ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("typed", [
    "4/10\\,", "4/10\\;", "4/10\\!", "4/10\\quad", "4/10\\qquad", "\\,4/10", "4\\,/10", "4/\\,10",
    "$4/10\\,$", "\\dfrac{4}{10}\\,", "4/10 \\quad ",
])
def test_unreduced_fraction_with_thin_spaces_is_still_unreduced(typed):
    assert check_open_answer(typed, "$\\dfrac{2}{5}$") is False


@pytest.mark.parametrize("typed, key", [
    ("42/10\\;", "$\\dfrac{21}{5}$"),
    ("3 7/4\\,", "$4 \\dfrac{3}{4}$"),
    ("3\\, 7/4", "$4 \\dfrac{3}{4}$"),
    ("5/1\\quad", "$5$"),
])
def test_other_unreduced_forms_with_thin_spaces_are_rejected(typed, key):
    assert check_open_answer(typed, key) is False


@pytest.mark.parametrize("typed, key", [("2/5\\,", "$\\dfrac{2}{5}$"), ("4/10\\,", "$\\dfrac{4}{10}$"), ("0,4\\;", "$\\dfrac{2}{5}$")])
def test_thin_spaces_do_not_break_a_valid_answer(typed, key):
    assert check_open_answer(typed, key) is True


def test_fraction_shape_sees_everything_exact_number_reads_as_a_fraction():
    """Fuzz: if exact_number reads a text as a fraction / mixed number, _fraction_shape must too."""
    rng = random.Random(20261013)
    tokens = ["4", "10", "2", "/", " ", "\\,", "\\;", "\\!", "\\quad", "\\qquad", "$", "-", "−", "\\dfrac", "\\frac",
              "\\tfrac", "{", "}", "{,}", ",", "\\dfrac{4}{10}", "3 7/4", "\\ ", "\u00a0"]
    checked = 0
    for _ in range(40000):
        text = "".join(rng.choice(tokens) for _ in range(rng.randint(1, 7)))
        if av.exact_number(text) is not None and ("/" in text or "frac" in text) and "," not in text.replace("\\,", ""):
            assert au._fraction_shape(text) is not None, repr(text)
            checked += 1
    assert checked > 200


# ── D. one verdict and one score ──────────────────────────────────────────────────────────────────────────────
def test_submit_stores_the_verdict_of_every_task(client, db):
    exam = _exam(db, [_task(task_id="T1"), _task(task_id="T2")])
    session = make_session_in_db(db, exam)
    assert client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={"answers": {"T1": "1/2", "T2": "$\\dfrac{1}{3}$"}}).status_code == 200
    db.refresh(session)
    marker = session.distractor_analytics[GRADING_KEY]
    assert marker == {"v": GRADER_VERSION, "verdicts": {"T1": True, "T2": False}}
    assert session.distractor_analytics["T2"]["hit"] is True              # the distractor hit is kept next to it
    assert (session.score, session.correct_count) == (10, 1)


def test_results_analytics_and_report_show_the_stored_verdict_even_if_the_grader_changes_later(client, db, monkeypatch):
    exam = _exam(db, [_task()])
    session = make_session_in_db(db, exam)
    client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={"answers": {"T1": "1/2"}})
    client.post(f"/api/v1/exams/{exam.id}/close")
    monkeypatch.setattr(grading, "check_open_answer", lambda *_: False)        # a stricter grader shipped afterwards
    results = client.get(f"/api/v1/exam-sessions/{session.id}/results").json()
    assert results["tasks"][0]["is_correct"] is True and results["correct_count"] == 1 and results["score"] == 10
    assert client.get(f"/api/v1/exams/{exam.id}/analytics").json()["task_analytics"][0]["correct_count"] == 1
    db.refresh(session)
    assert _aggregate(exam, [session])["task_analytics"][0]["correct_count"] == 1


def _legacy_session(db, exam, answers, score=0, correct=0, hits=None):
    session = make_session_in_db(db, exam, status="submitted")
    session.answers = answers
    session.score, session.correct_count, session.total_tasks, session.max_score = score, correct, len(exam.variants[0]["tasks"]), 10 * len(exam.variants[0]["tasks"])
    session.distractor_analytics = hits
    db.commit()
    return session


def test_legacy_session_is_regraded_once_with_its_score(client, db):
    """Submitted by the old grader (no marker): 1/2 was 0 points.  The result page must not say 'correct' with 0 points."""
    exam = _exam(db, [_task()])
    session = _legacy_session(db, exam, {"T1": "1/2"})
    assert stored_verdicts(session.distractor_analytics) is None
    client.post(f"/api/v1/exams/{exam.id}/close")
    results = client.get(f"/api/v1/exam-sessions/{session.id}/results").json()
    assert results["tasks"][0]["is_correct"] is True
    assert (results["correct_count"], results["score"], results["percentage"]) == (1, 10, 100.0)
    db.refresh(session)
    assert (session.score, session.correct_count) == (10, 1)
    assert session.distractor_analytics[GRADING_KEY]["v"] == GRADER_VERSION
    assert client.get(f"/api/v1/exams/{exam.id}/analytics").json()["avg_score_pct"] == 100.0


def test_legacy_session_in_choice_mode_keeps_its_numbers_and_its_hits(client, db):
    exam = _exam(db, [_task(task_id="T1"), _task(task_id="T2")], mc_mode="auto")
    hit = {"hit": True, "distractor_value": "$2$", "error_type": "x", "explanation": "e"}
    session = _legacy_session(db, exam, {"T1": "$\\dfrac{1}{2}$", "T2": "$2$"}, score=10, correct=1, hits={"T2": hit})
    client.post(f"/api/v1/exams/{exam.id}/close")
    results = client.get(f"/api/v1/exam-sessions/{session.id}/results").json()
    assert [t["is_correct"] for t in results["tasks"]] == [True, False]
    assert (results["correct_count"], results["score"]) == (1, 10)
    db.refresh(session)
    assert session.distractor_analytics["T2"] == hit


def test_regrade_drops_the_hit_of_a_task_that_is_now_right(client, db):
    exam = _exam(db, [_task()])
    hit = {"hit": True, "distractor_value": "1/2", "error_type": "x", "explanation": "e"}
    session = _legacy_session(db, exam, {"T1": "1/2"}, hits={"T1": hit})
    client.post(f"/api/v1/exams/{exam.id}/close")
    client.get(f"/api/v1/exam-sessions/{session.id}/results")
    db.refresh(session)
    assert "T1" not in session.distractor_analytics and session.correct_count == 1


def test_a_marker_of_another_grader_version_is_regraded(client, db):
    exam = _exam(db, [_task()])
    session = _legacy_session(db, exam, {"T1": "1/2"}, hits={GRADING_KEY: {"v": GRADER_VERSION - 1, "verdicts": {"T1": False}}})
    client.post(f"/api/v1/exams/{exam.id}/close")
    assert client.get(f"/api/v1/exam-sessions/{session.id}/results").json()["tasks"][0]["is_correct"] is True


@pytest.mark.parametrize("junk", [None, [], ["T1"], [{"hit": True}], "garbage", "", 0, 7])
def test_a_legacy_column_that_is_not_a_dict_is_stale_without_hits_not_an_error(client, db, junk):
    exam = _exam(db, [_task()])
    session = _legacy_session(db, exam, {"T1": "1/2"}, hits=None)
    session.distractor_analytics = junk
    db.commit()
    assert grading.regrade_if_stale(session, exam) is True
    assert session.distractor_analytics == {GRADING_KEY: {"v": GRADER_VERSION, "verdicts": {"T1": True}}}
    assert (session.score, session.correct_count) == (10, 1)
    assert grading.regrade_if_stale(session, exam) is False             # once


@pytest.mark.parametrize("junk", [None, ["x"], "garbage"])
def test_results_and_analytics_survive_a_junk_column(client, db, junk):
    exam = _exam(db, [_task()])
    session = _legacy_session(db, exam, {"T1": "1/2"})
    session.distractor_analytics = junk
    db.commit()
    client.post(f"/api/v1/exams/{exam.id}/close")
    results = client.get(f"/api/v1/exam-sessions/{session.id}/results")
    assert results.status_code == 200 and results.json()["tasks"][0]["is_correct"] is True
    assert client.get(f"/api/v1/exams/{exam.id}/analytics").status_code == 200
    db.refresh(session)
    assert _aggregate(exam, [session])["avg_score_pct"] == 100.0


def test_report_aggregate_uses_the_regraded_score_of_a_legacy_session(db):
    exam = _exam(db, [_task()])
    session = _legacy_session(db, exam, {"T1": "1/2"})
    aggregate = _aggregate(exam, [session])
    assert aggregate["avg_score_pct"] == 100.0 and aggregate["task_analytics"][0]["correct_count"] == 1


def test_the_marker_is_not_mistaken_for_a_distractor_hit(client, db):
    exam = _exam(db, [_task()], mc_mode="auto")
    session = make_session_in_db(db, exam)
    client.post(f"/api/v1/exam-sessions/{session.id}/submit", json={"answers": {"T1": "$2$"}})
    client.post(f"/api/v1/exams/{exam.id}/close")
    analytics = client.get(f"/api/v1/exams/{exam.id}/analytics").json()
    hits = analytics["task_analytics"][0]["distractor_hits"]
    assert len(hits) == 1 and hits[0]["count"] == 1
