"""WC-B EX-47: the lookup of an exam by its public code compares the indexed column itself (not ``upper(column)``, which cannot use the index).

Codes are always issued upper-case (A-Z0-9), the input is stripped and upper-cased in Python: «  ex12abc  » still finds EX12ABC."""
from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from src.repositories.exam_repo import ExamRepository
from tests.conftest import make_exam_in_db
from tests.sql_probe import count_statements


def test_the_query_has_no_function_over_the_column(db):
    exam = make_exam_in_db(db, status="lobby")
    exam.access_code = "EX12ABCD"
    db.commit()
    db.expunge_all()
    with count_statements() as sql:
        found = ExamRepository(db).get_by_access_code("EX12ABCD", without_tasks=True)
    assert found is not None and found.id == "EXAM_TEST01"
    where = sql.selects("generated_exams")[0].split(" WHERE ", 1)[1]
    print(f"\nEX-47 WHERE: {where}")
    assert "upper(" not in where.lower() and "generated_exams.access_code = " in where


def test_the_postgres_text_of_the_condition_is_an_index_equality(db):
    q = ExamRepository(db)._query(True).filter_by(access_code="X")
    sql = str(q.statement.compile(dialect=postgresql.dialect())).lower()
    assert "upper(" not in sql


@pytest.mark.parametrize("typed", ["EX12ABCD", "ex12abcd", "  ex12abcd \n", "Ex12AbCd"])
def test_every_spelling_of_the_code_finds_the_same_exam(db, typed):
    exam = make_exam_in_db(db, status="lobby")
    exam.access_code = "EX12ABCD"
    db.commit()
    assert ExamRepository(db).get_by_access_code(typed).id == exam.id


@pytest.mark.parametrize("typed", ["", None, "EX12ABCE", "EX12ABC", "   "])
def test_a_wrong_or_empty_code_finds_nothing(db, typed):
    exam = make_exam_in_db(db, status="lobby")
    exam.access_code = "EX12ABCD"
    db.commit()
    assert ExamRepository(db).get_by_access_code(typed) is None


def test_a_code_the_service_issues_is_upper_case(db):
    from src.api.router import _generate_access_code

    for _ in range(200):
        code = _generate_access_code(db)
        assert code == code.upper() and code.startswith("EX") and len(code) == 10
