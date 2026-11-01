"""LESSONS 76 / 82: a test never takes its day from the real clock.

A fixture or an assertion built from ``date.today()`` or from "now - N days" passes on most days and fails on the 30th, the 31st, the 1st or on
New Year (a lesson falls before the enrolment, a payment lands in another month, a window starts a day later). The business day of the code under
test is business_today() from tests/conftest.py; any other calendar date is a literal.

Forbidden in every file under ``tests/`` (docstrings and comments are not code and are not looked at):
  1. ``date.today()``, ``datetime.today()`` and ``datetime.now()`` without a zone - the machine's day, never the business day;
  2. any real-clock call (``datetime.now(tz)``, ``datetime.utcnow()``, ``utcnow()``) plus or minus a ``timedelta`` of days or weeks - a calendar
     distance from the real clock; an instant in seconds / minutes / hours is fine (a token or an invite that expired a minute ago);
  3. the calendar day taken from a real-clock call (``datetime.utcnow().date()``): that is the UTC day, the business day is ``business_today()``.
Exceptions are listed below with the reason; a new one needs a reason a reviewer can check.
"""
import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
CLOCK_OWNERS = ("date", "datetime", "real_datetime")
CLOCK_NAMES = {"utcnow"}

# (file name, rule number) -> reason. The rule-2 exceptions are about an INSTANT or a rolling retention, not about a calendar month.
ALLOWED = {
    ("class_load.py", 3): "the load script asks for the Tashkent day on purpose (datetime.now(ZoneInfo(\"Asia/Tashkent\")).date()) - it is not a test and has no pinned clock",
}


def _is_real_clock_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr in {"today", "now", "utcnow"}:
        owner = ast.unparse(f.value)
        return owner.endswith(CLOCK_OWNERS)
    return isinstance(f, ast.Name) and f.id in CLOCK_NAMES


def _is_naive(node: ast.Call) -> bool:
    f = node.func
    return isinstance(f, ast.Attribute) and (f.attr == "today" or (f.attr == "now" and not node.args and not node.keywords))


def _days_timedelta(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and ast.unparse(sub.func).endswith("timedelta"):
            if any(k.arg in {"days", "weeks"} for k in sub.keywords) or sub.args:
                return True
    return False


def violations(source: str, filename: str = "<src>"):
    """[(line, rule, text)] of the file. Rule 1: naive today/now. Rule 2: a real clock +- days/weeks. Rule 3: ``.date()`` of a real clock."""
    found = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if _is_real_clock_call(node) and _is_naive(node):
            found.append((node.lineno, 1, ast.unparse(node)))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "date"
              and _is_real_clock_call(node.func.value)):
            found.append((node.lineno, 3, ast.unparse(node)))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            sides = (node.left, node.right)
            clock = [s for s in sides if any(_is_real_clock_call(c) for c in ast.walk(s))]
            delta = [s for s in sides if s not in clock and _days_timedelta(s)]
            if clock and delta:
                found.append((node.lineno, 2, ast.unparse(node)[:110]))
    return found


def test_the_guard_catches_the_real_clock():
    assert violations("x = date.today()")
    assert violations("x = datetime.now()")
    assert violations('x = __import__("datetime").datetime.now() - timedelta(days=60)')
    assert violations("x = datetime.utcnow() - timedelta(days=5)")
    assert violations("x = datetime.now(timezone.utc) + timedelta(weeks=1)")
    assert violations("x = utcnow() - timedelta(days=31)")
    assert violations("x = datetime.utcnow().date() + timedelta(days=1)")
    assert [r for _, r, _ in violations("x = datetime.utcnow().date()")] == [3]
    assert [r for _, r, _ in violations('x = datetime.now(ZoneInfo("Asia/Tashkent")).date()')] == [3]
    assert not violations("x = business_today().isoformat()")
    assert violations("x = date.today().replace(year=date.today().year + 1)")
    assert not violations("x = datetime.now(timezone.utc) - timedelta(minutes=30)")       # an instant, not a calendar distance
    assert not violations("x = datetime.utcnow()")
    assert not violations("x = business_today() - timedelta(days=3)")
    assert not violations("x = ENROLLED_SINCE + timedelta(days=40)")
    assert not violations("x = func.now()")
    assert not violations('"""date.today() in a docstring"""  # datetime.now()')


def test_no_test_takes_the_day_from_the_real_clock():
    bad = []
    for path in sorted(TESTS.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        for line, rule, text in violations(path.read_text(encoding="utf-8"), path.name):
            if (path.name, rule) not in ALLOWED:
                bad.append(f"{path.relative_to(TESTS)}:{line}: rule {rule}: {text}")
    assert not bad, (
        "A test must not take its date from the real clock (LESSONS 76/82): use business_today() from tests/conftest.py "
        "or a literal date.\n" + "\n".join(bad)
    )


def test_every_exception_still_has_a_reason_and_a_use():
    """An exception that no longer matches anything is dead weight: it would hide a future bomb in the same file."""
    used = set()
    for path in sorted(TESTS.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        for _, rule, _ in violations(path.read_text(encoding="utf-8"), path.name):
            used.add((path.name, rule))
    for key, reason in ALLOWED.items():
        assert reason.strip(), key
        assert key in used, f"the exception {key} no longer matches anything - delete it"
