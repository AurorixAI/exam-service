"""Wave E, E-D (EX-43 / РАСХ-11): the rule «which lessons an exam may be bound to» lives twice - ``isLessonBindable`` in the front dialog and ``_lesson_is_bindable`` here.
``fixtures/lesson_bindable_golden.json`` is the table both must agree on (the front test reads the same file).  A blank end time - empty or only spaces - is NO end time:
the server does not judge such a lesson over (the dialog did: ``!endTime`` is false for «   » and ``clockMinutes`` read it as 0)."""
import json
import pathlib
from datetime import date, timedelta

import pytest

from src.api.router import _clock_minutes, _lesson_is_bindable

GOLDEN = json.loads((pathlib.Path(__file__).parent / "fixtures" / "lesson_bindable_golden.json").read_text(encoding="utf-8"))
TODAY = date(2026, 11, 4)
DAYS = {"today": TODAY, "yesterday": TODAY - timedelta(days=1), "tomorrow": TODAY + timedelta(days=1)}


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: c["id"])
def test_the_rule_gives_the_verdict_of_the_table(case):
    hours, minutes = case["now"].split(":")
    got = _lesson_is_bindable(lesson_date=DAYS[case["day"]].isoformat(), end_time=case["end_time"], lesson_status=case["status"], today=TODAY,
                              now_minutes=int(hours) * 60 + int(minutes))
    assert got is case["bindable"]


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t", "\n ", None])
def test_a_blank_end_time_is_no_end_time(blank):
    assert _clock_minutes(blank) is None


def test_the_table_names_where_the_front_differs_today():
    assert [c["id"] for c in GOLDEN["cases"] if "ts_today" in c] == ["no-end-spaces", "no-end-one-space"]
