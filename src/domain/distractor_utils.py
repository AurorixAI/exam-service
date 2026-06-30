"""
Distractor text helpers — align content-service (error_logic) with exam UI (explanation).

content-service stores mistake text as error_logic and/or explanation (legacy).
exam-service always exposes a single canonical string via explanation in API responses.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from src.domain.answer_utils import normalise_answer


def match_distractor(student: str, distractor_meta: list) -> Optional[Dict[str, Any]]:
    """Return distractor dict if student answer matches a distractor value."""
    if not distractor_meta or not student:
        return None
    s = normalise_answer(student)
    for d in distractor_meta:
        if not isinstance(d, dict):
            continue
        d_val = normalise_answer(str(d.get("value", "")))
        if s == d_val:
            return d
        try:
            if abs(float(s) - float(d_val)) < 0.01:
                return d
        except (ValueError, TypeError):
            pass
    return None


def distractor_text(item: Optional[Dict[str, Any]]) -> str:
    """Canonical student-mistake description (error_logic preferred, same as content gate)."""
    if not item or not isinstance(item, dict):
        return ""
    logic = str(item.get("error_logic") or "").strip()
    explanation = str(item.get("explanation") or "").strip()
    return logic or explanation


def normalize_distractor_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure both API fields are populated for teacher draft / task payloads."""
    if not isinstance(item, dict):
        return item
    out = dict(item)
    text = distractor_text(out)
    if text:
        out["explanation"] = text
        if not str(out.get("error_logic") or "").strip():
            out["error_logic"] = text
    return out


def normalize_distractor_meta(
    meta: Optional[List[Dict[str, Any]]],
) -> Optional[List[Dict[str, Any]]]:
    if not meta:
        return meta
    return [normalize_distractor_item(d) for d in meta if isinstance(d, dict)]


def build_analytics_hit(distractor: Dict[str, Any]) -> Dict[str, Any]:
    """Payload stored in session.distractor_analytics after submit."""
    return {
        "hit": True,
        "distractor_value": str(distractor.get("value", "")),
        "error_type": distractor.get("error_type", ""),
        "explanation": distractor_text(distractor),
    }


def resolve_analytics_explanation(
    hit: Optional[Dict[str, Any]],
    *,
    student_answer: str = "",
    distractor_meta: Optional[List[Dict[str, Any]]] = None,
    match_fn: Optional[Callable[[str, list], Optional[Dict[str, Any]]]] = None,
) -> str:
    """
    Resolve explanation for analytics/results.

    Uses stored hit first; for legacy sessions with empty explanation,
    re-matches student answer against task distractor_meta.
    """
    text = distractor_text(hit)
    if text:
        return text
    if student_answer and distractor_meta:
        matcher = match_fn or match_distractor
        matched = matcher(student_answer, distractor_meta)
        if matched:
            return distractor_text(matched)
    return ""
