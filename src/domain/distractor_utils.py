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
    """Canonical student-mistake description (error_logic_latex preferred, then error_logic, then explanation)."""
    if not item or not isinstance(item, dict):
        return ""
    latex = str(item.get("error_logic_latex") or "").strip()
    logic = str(item.get("error_logic") or "").strip()
    explanation = str(item.get("explanation") or "").strip()
    return latex or logic or explanation


def distractor_latex_text(item: Optional[Dict[str, Any]]) -> str:
    """Canonical LaTeX student-mistake description."""
    if not item or not isinstance(item, dict):
        return ""
    latex = str(item.get("error_logic_latex") or "").strip()
    return latex or distractor_text(item)


def normalize_distractor_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure error_logic_latex, error_logic, and explanation are all populated."""
    if not isinstance(item, dict):
        return item
    out = dict(item)
    latex_text = distractor_latex_text(out)
    plain_text = (
        str(out.get("error_logic") or "").strip()
        or str(out.get("explanation") or "").strip()
        or latex_text
    )
    if latex_text:
        out["error_logic_latex"] = latex_text
    if plain_text:
        if not str(out.get("error_logic") or "").strip():
            out["error_logic"] = plain_text
        if not str(out.get("explanation") or "").strip():
            out["explanation"] = plain_text
    return out


def normalize_distractor_meta(
    meta: Optional[List[Dict[str, Any]]],
) -> Optional[List[Dict[str, Any]]]:
    if not meta:
        return meta
    return [normalize_distractor_item(d) for d in meta if isinstance(d, dict)]


def normalize_answer_options(
    options: Optional[List[Any]],
) -> Optional[List[str]]:
    if not options or not isinstance(options, list):
        return None
    res = []
    for opt in options:
        if isinstance(opt, str):
            res.append(opt)
        elif isinstance(opt, dict):
            val = opt.get("text") or opt.get("value") or opt
            res.append(str(val))
        else:
            res.append(str(opt))
    return res


def build_analytics_hit(distractor: Dict[str, Any]) -> Dict[str, Any]:
    """Payload stored in session.distractor_analytics after submit."""
    return {
        "hit": True,
        "distractor_value": str(distractor.get("value", "")),
        "error_type": distractor.get("error_type", ""),
        "explanation": distractor_text(distractor),
        "error_logic": str(distractor.get("error_logic") or "").strip() or distractor_text(distractor),
        "error_logic_latex": str(distractor.get("error_logic_latex") or "").strip() or distractor_latex_text(distractor),
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
