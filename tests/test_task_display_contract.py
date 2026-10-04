"""Canonical column selection and immutable saved-report projections."""
from copy import deepcopy
import json

import pytest

from src.domain.report_generator import _aggregate, _build_deterministic_fallback, _build_deepseek_prompt
from src.domain.task_display import bind_report_task_statements, question_display_fields, report_display_projection
from src.domain.print_pdf import _task_text
from src.repositories.exam_repo import ExamReportRepository
from tests.conftest import make_exam_in_db, make_session_in_db, make_task


@pytest.mark.parametrize("text,latex", [
    ("Старое подробное условие. " * 20, "Найдите $x$."),
    ("Вычислите значение выражения.", r"$\frac{3}{4}$"),
    ("Решите $x^2 = 9$.", "$x^{2} = 9$"),
    ("Решите уравнение и проверьте ответ.", "Solve $x+1=2$."),
    ("Другой текст", r"$x^{\dfrac{5}{6}}$"),
])
def test_print_and_report_choose_canonical_column(text, latex):
    task = {"question_text": text, "question_latex": latex}
    assert _task_text(task) == latex
    fields = question_display_fields(task)
    assert fields["question_text"] == text.strip()
    assert fields["question_latex"] == latex
    assert fields["question_preview"] == latex[:180]
    assert task == {"question_text": text, "question_latex": latex}


@pytest.mark.parametrize("latex", [None, "", "   "])
def test_missing_canonical_column_uses_legacy_statement(latex):
    task = {"question_text": "Найдите $x$.", "question_latex": latex}
    assert _task_text(task) == "Найдите $x$."
    assert question_display_fields(task)["question_preview"] == "Найдите $x$."


def test_narrative_binds_by_task_id_instead_of_model_wording():
    task = {"task_id": "T1", "question_text": "Старое условие", "question_latex": "Найдите $x$."}
    narrative = {"problem_tasks_analysis": [{"task_id": "T1", "question_text": "Модель переписала условие", "teacher_advice": "Совет"}]}
    before = deepcopy(narrative)
    result = bind_report_task_statements(narrative, [task])
    assert result["problem_tasks_analysis"][0]["question_latex"] == task["question_latex"]
    assert result["problem_tasks_analysis"][0]["question_text"] == task["question_text"]
    assert result["problem_tasks_analysis"][0]["teacher_advice"] == "Совет"
    assert narrative == before


def test_historical_reports_never_guess_a_task_by_order_or_ambiguous_text():
    tasks = [{"task_id": "T1", "question_text": "Общее условие", "question_latex": "$x=1$"},
             {"task_id": "T2", "question_text": "Общее условие", "question_latex": "$x=2$"}]
    entries = [{"question_text": "Общее условие"}, {"task_id": "missing", "question_text": "$x=1$"},
               {"question_text": "Похожее условие"}]
    result = bind_report_task_statements({"problem_tasks_analysis": entries}, tasks)
    for item in result["problem_tasks_analysis"]:
        assert item["statement_unavailable"] is True
        assert "question_text" not in item
        assert "question_latex" not in item
    assert entries[0]["question_text"] == "Общее условие"


def test_report_http_uses_exam_snapshot_and_does_not_modify_cached_report(client, db):
    task = make_task(text="Подробный старый текст")
    task["question_latex"] = "Найдите $x$."
    exam = make_exam_in_db(db, tasks=[task])
    stored_analytics = [{"task_id": task["task_id"], "question_preview": "Подробный старый текст", "attempts": 1}]
    stored_narrative = json.dumps({"problem_tasks_analysis": [{"question_text": task["question_text"], "teacher_advice": "Совет"}]})
    saved = ExamReportRepository(db).upsert(
        exam.id, "ER_DISPLAY", total_students=1, submitted_count=1, avg_score_pct=0,
        pass_rate_pct=0, task_analytics=stored_analytics, top_errors=[], narrative_ru=stored_narrative,
        narrative_generated=True, is_final=True,
    )
    response = client.get(f"/api/v1/exams/{exam.id}/report")
    assert response.status_code == 200
    result = response.json()
    assert result["task_analytics"][0]["question_latex"] == task["question_latex"]
    assert json.loads(result["narrative_ru"])["problem_tasks_analysis"][0]["question_latex"] == task["question_latex"]
    db.refresh(saved)
    assert saved.task_analytics == stored_analytics
    assert saved.narrative_ru == stored_narrative
    assert exam.variants[0]["tasks"][0] == task


def test_live_analytics_and_deterministic_report_keep_question_latex(client, db):
    task = make_task(text="Подробный старый текст")
    task["question_latex"] = "Solve $x+1=2$."
    exam = make_exam_in_db(db, tasks=[task])
    session = make_session_in_db(db, exam, status="submitted")
    session.answers = {task["task_id"]: "999"}
    session.score = 0
    db.commit()
    response = client.get(f"/api/v1/exams/{exam.id}/analytics")
    assert response.status_code == 200
    assert response.json()["task_analytics"][0]["question_latex"] == task["question_latex"]
    agg = _aggregate(exam, [session])
    assert agg["task_analytics"][0]["question_latex"] == task["question_latex"]
    narrative = _build_deterministic_fallback(exam.title, 8, agg)
    assert narrative["problem_tasks_analysis"][0]["task_id"] == task["task_id"]
    assert narrative["problem_tasks_analysis"][0]["question_latex"] == task["question_latex"]
    prompt = _build_deepseek_prompt(exam.title, 8, agg)
    assert f'Условие: «{task["question_latex"]}»' in prompt
    assert f'task_id: {task["task_id"]}' in prompt


def test_non_json_historical_narrative_is_preserved():
    text = "Исторический текст отчёта"
    assert report_display_projection([], [], text) == ([], text)
