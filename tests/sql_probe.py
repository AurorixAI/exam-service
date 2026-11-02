"""Statement counter for the wave-C tests: how many SQL statements a request sent and which of them read the big JSONB columns.

Transaction control of the savepoint fixture (SAVEPOINT / RELEASE / BEGIN) is not a query and is not counted.  The counter listens on
the engine of ``conftest`` - the one every ``db`` fixture session is bound to."""
from __future__ import annotations

import re
from contextlib import contextmanager

from sqlalchemy import event

from tests.conftest import engine


class Statements:
    def __init__(self):
        self.sql: list[str] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        head = statement.lstrip()
        if head.startswith(("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT", "BEGIN", "COMMIT", "ROLLBACK")):
            return
        self.sql.append(" ".join(statement.split()))

    @property
    def count(self) -> int:
        return len(self.sql)

    def selects(self, table: str) -> list[str]:
        return [s for s in self.sql if s.startswith("SELECT") and re.search(rf"\bFROM {table}\b", s)]

    def reading(self, table: str, column: str) -> int:
        """SELECTs of ``table`` that carry ``column`` in their select list (before FROM) - one per read of the big JSON."""
        n = 0
        for s in self.selects(table):
            select_list = s.split(" FROM ", 1)[0]
            if re.search(rf"\b{table}\.{column}\b(?!_)", select_list):
                n += 1
        return n


@contextmanager
def count_statements():
    rec = Statements()
    event.listen(engine, "before_cursor_execute", rec)
    try:
        yield rec
    finally:
        event.remove(engine, "before_cursor_execute", rec)
