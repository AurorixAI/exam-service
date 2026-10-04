"""Question projections from the saved exam, without rewriting bank content."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def question_display_fields(task: dict[str, Any]) -> dict[str, str]:
    text = (task.get("question_text") or "").strip()
    latex = (task.get("question_latex") or "").strip()
    return {
        "question_text": text,
        "question_latex": latex,
        "question_preview": (latex or text)[:180],
    }


def bind_report_task_statements(
    narrative: dict[str, Any], tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind by exact task id, or one unique exact statement in older reports.

    Never match by order, topic, length, or model paraphrase. An unbound model
    statement is omitted from the API projection; advice and the stored report
    remain intact. It must not be presented as the canonical exam question.
    """
    result = deepcopy(narrative)
    by_id = {str(t["task_id"]): t for t in tasks if t.get("task_id")}
    entries = result.get("problem_tasks_analysis")
    if not isinstance(entries, list):
        return result
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        task_id = entry.get("task_id")
        task = by_id.get(str(task_id)) if task_id else None
        if not task_id:
            statement = (entry.get("question_text") or "").strip()
            matches = [t for t in by_id.values() if statement and statement in {
                (t.get("question_text") or "").strip(),
                (t.get("question_latex") or "").strip(),
            }]
            task = matches[0] if len(matches) == 1 else None
        if task is None:
            for field in ("question_text", "question_latex", "question_preview"):
                entry.pop(field, None)
            entry["statement_unavailable"] = True
            continue
        entry.update(question_display_fields(task))
        entry["task_id"] = task["task_id"]
        entry.pop("statement_unavailable", None)
    return result


def report_display_projection(
    variants: list[dict[str, Any]], analytics: list[dict[str, Any]], narrative: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Hydrate cached reports from their exam variants, never the live bank."""
    import json

    by_id: dict[str, dict[str, Any]] = {}
    for variant in variants:
        for task in variant.get("tasks", []):
            if task.get("task_id"):
                by_id.setdefault(str(task["task_id"]), task)
    projected = deepcopy(analytics)
    for item in projected:
        task = by_id.get(str(item.get("task_id")))
        if task is not None:
            item.update(question_display_fields(task))
    if narrative:
        try:
            parsed = json.loads(narrative)
        except (ValueError, TypeError):
            return projected, narrative
        if isinstance(parsed, dict):
            narrative = json.dumps(
                bind_report_task_statements(parsed, list(by_id.values())), ensure_ascii=False,
            )
    return projected, narrative
