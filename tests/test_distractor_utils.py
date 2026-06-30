"""Unit tests for distractor_utils."""
from src.domain.distractor_utils import (
    build_analytics_hit,
    distractor_text,
    normalize_distractor_item,
    normalize_distractor_meta,
    resolve_analytics_explanation,
)


def test_distractor_text_prefers_error_logic():
    assert distractor_text({
        "error_logic": "перепутал знак",
        "explanation": "типичная ошибка",
    }) == "перепутал знак"


def test_distractor_text_falls_back_to_explanation():
    assert distractor_text({
        "explanation": "ошибка при сложении дробей",
    }) == "ошибка при сложении дробей"


def test_distractor_text_empty():
    assert distractor_text({}) == ""
    assert distractor_text(None) == ""


def test_normalize_distractor_item_syncs_both_fields():
    out = normalize_distractor_item({"value": "5", "error_logic": "добавил единицу"})
    assert out["explanation"] == "добавил единицу"
    assert out["error_logic"] == "добавил единицу"


def test_normalize_distractor_meta():
    meta = normalize_distractor_meta([
        {"value": "3", "explanation": "вычел вместо сложения"},
    ])
    assert meta[0]["explanation"] == "вычел вместо сложения"
    assert meta[0]["error_logic"] == "вычел вместо сложения"


def test_build_analytics_hit():
    hit = build_analytics_hit({
        "value": "9/4",
        "error_type": "ai_generated",
        "error_logic": "не привёл к общему знаменателю",
    })
    assert hit == {
        "hit": True,
        "distractor_value": "9/4",
        "error_type": "ai_generated",
        "explanation": "не привёл к общему знаменателю",
    }


def test_resolve_analytics_explanation_from_legacy_meta():
    def match(student: str, meta: list):
        return meta[0] if student == "40" else None

    hit = {"hit": True, "distractor_value": "40", "error_type": "x", "explanation": ""}
    meta = [{"value": "40", "error_logic": "ошибка округления"}]
    assert resolve_analytics_explanation(
        hit,
        student_answer="40",
        distractor_meta=meta,
        match_fn=match,
    ) == "ошибка округления"
