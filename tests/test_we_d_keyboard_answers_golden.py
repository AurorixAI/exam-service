"""Wave E, E-D (decision 8, exam half): a typed (keyboard) answer is graded by ``answer_utils.check_open_answer``; the diagnostic service grades the same
table the same way (``answer_value.typed_answer_matches``).  ``fixtures/keyboard_answers_golden.json`` is the copy of the diag table - the rule of exam does NOT
change here, the table only pins it (0 disagreements).  Change a verdict only together with the same file in diagnostic-service and the owner's word."""
import json
import pathlib

import pytest

from src.domain.answer_utils import check_open_answer

GOLDEN = json.loads((pathlib.Path(__file__).parent / "fixtures" / "keyboard_answers_golden.json").read_text(encoding="utf-8"))


def pairs(kind):
    return [pytest.param(case["key"], typed, id=f"{case['id']}|{typed}") for case in GOLDEN["cases"] for typed in case.get(kind, [])]


@pytest.mark.parametrize("key, typed", pairs("accept"))
def test_equivalent_notations_are_accepted(key, typed):
    assert check_open_answer(typed, key) is True


@pytest.mark.parametrize("key, typed", pairs("reject"))
def test_other_values_and_unreduced_notations_are_refused(key, typed):
    assert check_open_answer(typed, key) is False


@pytest.mark.parametrize("case", GOLDEN["contested"], ids=lambda c: f"{c['key']}|{c['typed']}")
def test_the_contested_cases_have_the_verdict_the_table_records(case):
    assert case["diag"] == case["exam"]                                    # the two services agree on every recorded case
    assert check_open_answer(case["typed"], case["key"]) is case["exam"]


def test_the_table_is_not_empty():
    assert sum(len(c.get("accept", [])) + len(c.get("reject", [])) for c in GOLDEN["cases"]) >= 50
