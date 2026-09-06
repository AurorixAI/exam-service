"""
Unit tests — pure logic functions, no DB required.

Covers:
  - _check_answer (exact, decimal, fraction, case variants)
  - _normalise_answer
  - _difficulty_counts
  - _should_show_mc
  - _build_mc_options
  - _generate_access_code
  - ExamServiceError hierarchy
  - Settings defaults
"""
from __future__ import annotations

import sys, os
import asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("APP_ENV", "testing")

import pytest
from src.api.router import (
    _check_answer,
    _normalise_answer,
    _difficulty_counts,
    _should_show_mc,
    _build_mc_options,
    _generate_access_code,
)
from src.core.exceptions import (
    ExamServiceError,
    ExamNotFoundError,
    ExamNotActiveError,
    SessionNotFoundError,
    SessionAlreadySubmittedError,
    NoTasksAvailableError,
    InvalidExamConfigError,
)
from src.core.config import get_settings, Settings
from src.core.auth import ExamActor, require_group_access
from fastapi import HTTPException


class TestGroupAccessGuards:
    def test_legacy_non_uuid_group_is_treated_as_missing(self):
        """A stale legacy row must not make a teacher's whole library fail."""
        actor = ExamActor(
            user_id="teacher-1",
            org_id="org-1",
            role="teacher",
            auth_headers={},
        )

        async def check() -> None:
            with pytest.raises(HTTPException) as error:
                await require_group_access(actor, "GRP_test")
            assert error.value.status_code == 404

        asyncio.run(check())


# ═══════════════════════════════════════════════════════════════════════════
# _normalise_answer
# ═══════════════════════════════════════════════════════════════════════════

class TestNormaliseAnswer:
    def test_strips_whitespace(self):
        assert _normalise_answer("  42  ") == "42"

    def test_lowercases(self):
        assert _normalise_answer("ABC") == "abc"

    def test_comma_to_dot(self):
        assert _normalise_answer("3,14") == "3.14"

    def test_empty_string(self):
        assert _normalise_answer("") == ""

    def test_combined(self):
        assert _normalise_answer("  3,14  ") == "3.14"


# ═══════════════════════════════════════════════════════════════════════════
# _check_answer
# ═══════════════════════════════════════════════════════════════════════════

class TestCheckAnswer:
    # ── Exact match ──
    def test_exact_integer_match(self):
        assert _check_answer("42", "42") is True

    def test_exact_integer_mismatch(self):
        assert _check_answer("41", "42") is False

    # ── Float tolerance (< 0.01) ──
    def test_float_close_match(self):
        assert _check_answer("3.14", "3.14") is True

    def test_float_within_tolerance(self):
        assert _check_answer("3.145", "3.145") is True

    def test_float_outside_tolerance(self):
        assert _check_answer("3.0", "3.5") is False

    # ── Comma as decimal separator ──
    def test_comma_decimal(self):
        assert _check_answer("3,14", "3.14") is True

    # ── Leading/trailing whitespace ──
    def test_whitespace_student_answer(self):
        assert _check_answer(" 42 ", "42") is True

    # ── String answer (non-numeric) ──
    def test_text_exact(self):
        assert _check_answer("да", "да") is True

    def test_text_case_insensitive(self):
        assert _check_answer("ДА", "да") is True

    def test_text_mismatch(self):
        assert _check_answer("нет", "да") is False

    # ── Empty answer ──
    def test_empty_student_answer(self):
        assert _check_answer("", "42") is False

    def test_both_empty(self):
        assert _check_answer("", "") is True

    # ── Negative numbers ──
    def test_negative_correct(self):
        assert _check_answer("-5", "-5") is True

    def test_negative_mismatch(self):
        assert _check_answer("5", "-5") is False


# ═══════════════════════════════════════════════════════════════════════════
# _difficulty_counts
# ═══════════════════════════════════════════════════════════════════════════

class TestDifficultyCounts:
    def test_total_matches_tasks_count(self):
        dist = {"easy": 30, "medium": 50, "hard": 20}
        counts = _difficulty_counts(10, dist)
        assert sum(counts.values()) == 10

    def test_hard_gets_remainder(self):
        dist = {"easy": 33, "medium": 34, "hard": 33}
        counts = _difficulty_counts(9, dist)
        assert sum(counts.values()) == 9

    def test_keys_are_ABC(self):
        counts = _difficulty_counts(10, {"easy": 30, "medium": 50, "hard": 20})
        assert set(counts.keys()) == {"A", "B", "C"}

    def test_zero_tasks(self):
        counts = _difficulty_counts(0, {"easy": 30, "medium": 50, "hard": 20})
        assert sum(counts.values()) == 0

    def test_all_medium(self):
        counts = _difficulty_counts(10, {"easy": 0, "medium": 100, "hard": 0})
        assert counts["B"] == 10
        assert counts["A"] == 0
        assert counts["C"] == 0

    def test_single_task(self):
        counts = _difficulty_counts(1, {"easy": 50, "medium": 50, "hard": 0})
        assert sum(counts.values()) == 1


# ═══════════════════════════════════════════════════════════════════════════
# _should_show_mc
# ═══════════════════════════════════════════════════════════════════════════

class TestShouldShowMC:
    def _task(self, answer_type="exact_number", difficulty="B", has_options=True):
        return {
            "answer_type": answer_type,
            "difficulty": difficulty,
            "answer_options": ["1", "2", "3", "4"] if has_options else None,
            "distractor_meta": None,
        }

    def test_auto_mc_type_with_options(self):
        assert _should_show_mc(self._task("exact_number", "B"), "auto") is True

    def test_auto_ignores_difficulty(self):
        """Difficulty no longer gates MC: a hard task with options still shows them."""
        assert _should_show_mc(self._task("exact_number", "C"), "auto") is True

    def test_auto_no_options(self):
        assert _should_show_mc(self._task(has_options=False), "auto") is False

    def test_all_mc_with_options(self):
        assert _should_show_mc(self._task("exact_number", "C"), "all_mc") is True

    def test_all_mc_without_options(self):
        assert _should_show_mc(self._task(has_options=False), "all_mc") is False

    def test_all_open_always_false(self):
        assert _should_show_mc(self._task(), "all_open") is False

    def test_auto_true_false_type(self):
        assert _should_show_mc(self._task("true_false", "A"), "auto") is True

    def test_auto_ignores_answer_type(self):
        """Only the presence of options (and mc_mode) decides, not answer_type."""
        assert _should_show_mc(self._task("text", "A"), "auto") is True


# ═══════════════════════════════════════════════════════════════════════════
# _build_mc_options
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildMCOptions:
    def test_uses_answer_options_when_available(self):
        task = {
            "correct_answer": "1",
            "answer_options": ["1", "2", "3", "4"],
            "distractor_meta": None,
        }
        options = _build_mc_options(task, shuffle=False)
        assert set(options) == {"1", "2", "3", "4"}
        assert len(options) == 4

    def test_correct_answer_included_if_missing_from_options(self):
        task = {
            "correct_answer": "5",
            "answer_options": ["1", "2", "3"],
            "distractor_meta": None,
        }
        options = _build_mc_options(task, shuffle=False)
        assert "5" in options

    def test_uses_distractor_meta_as_fallback(self):
        task = {
            "correct_answer": "42",
            "answer_options": None,
            "distractor_meta": [
                {"value": "40"}, {"value": "44"}, {"value": "48"}
            ],
        }
        options = _build_mc_options(task, shuffle=False)
        assert "42" in options
        assert len(options) == 4  # correct + 3 distractors

    def test_empty_task_returns_empty_list(self):
        task = {"correct_answer": "", "answer_options": None, "distractor_meta": None}
        assert _build_mc_options(task, shuffle=False) == []

    def test_shuffle_is_seeded_and_stable_within_an_attempt(self):
        """One seed must always give one order; different seeds may differ.

        A student can re-fetch an active exam after a reconnect, so the option
        letters must not move within their attempt.
        """
        task = {
            "correct_answer": "1",
            "answer_options": ["1", "2", "3", "4"],
            "distractor_meta": None,
        }
        same_seed = {
            tuple(_build_mc_options(task, shuffle=True, seed="SES_1"))
            for _ in range(10)
        }
        assert len(same_seed) == 1

        across_seeds = {
            tuple(_build_mc_options(task, shuffle=True, seed=f"SES_{i}"))
            for i in range(25)
        }
        assert len(across_seeds) > 1
        assert all(set(order) == {"1", "2", "3", "4"} for order in across_seeds)

    def test_no_shuffle_keeps_source_order(self):
        task = {
            "correct_answer": "1",
            "answer_options": ["1", "2", "3", "4"],
            "distractor_meta": None,
        }
        assert _build_mc_options(task, shuffle=False) == ["1", "2", "3", "4"]


# ═══════════════════════════════════════════════════════════════════════════
# _generate_access_code
# ═══════════════════════════════════════════════════════════════════════════

class TestGenerateAccessCode:
    def test_starts_with_EX(self):
        code = _generate_access_code()
        assert code.startswith("EX")

    def test_length_is_10(self):
        # "EX" + 8 chars
        code = _generate_access_code()
        assert len(code) == 10

    def test_unique_codes(self):
        codes = {_generate_access_code() for _ in range(100)}
        # With 8 alphanumeric chars, collisions in 100 tries are essentially impossible
        assert len(codes) > 95

    def test_only_uppercase_alphanumeric(self):
        import string
        allowed = set(string.ascii_uppercase + string.digits)
        for _ in range(20):
            code = _generate_access_code()
            assert all(c in allowed for c in code[2:])


# ═══════════════════════════════════════════════════════════════════════════
# Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_base_is_exception(self):
        err = ExamServiceError("test")
        assert isinstance(err, Exception)
        assert str(err) == "test"

    def test_exam_not_found_inherits_base(self):
        err = ExamNotFoundError("EX123")
        assert isinstance(err, ExamServiceError)
        assert "EX123" in err.message
        assert err.code == "EXAM_NOT_FOUND"

    def test_exam_not_active(self):
        err = ExamNotActiveError("EX123")
        assert err.code == "EXAM_NOT_ACTIVE"

    def test_session_not_found(self):
        err = SessionNotFoundError("SES123")
        assert "SES123" in err.message

    def test_session_already_submitted(self):
        err = SessionAlreadySubmittedError("SES123")
        assert err.code == "SESSION_ALREADY_SUBMITTED"

    def test_no_tasks_available(self):
        err = NoTasksAvailableError("Нет задач")
        assert "Нет задач" in str(err)

    def test_invalid_exam_config(self):
        err = InvalidExamConfigError("Bad config")
        assert err.code == "INVALID_EXAM_CONFIG"


# ═══════════════════════════════════════════════════════════════════════════
# Settings
# ═══════════════════════════════════════════════════════════════════════════

class TestSettings:
    def test_defaults(self):
        s = Settings(
            database_url="sqlite:///:memory:",
            app_env="testing",
        )
        assert s.max_tasks_per_exam == 50
        assert s.max_variants == 30
        assert s.default_time_minutes == 45
        assert s.gemini_model == "gemini-2.5-flash"

    def test_is_development_false_by_default(self):
        s = Settings(database_url="sqlite:///:memory:", app_env="production")
        assert s.is_development is False

    def test_is_development_true(self):
        s = Settings(database_url="sqlite:///:memory:", app_env="development")
        assert s.is_development is True
