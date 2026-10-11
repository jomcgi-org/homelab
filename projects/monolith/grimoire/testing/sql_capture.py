"""Reusable SQL capture for proving campaign extraction stays out of knowledge."""

import re
from contextlib import contextmanager

from sqlalchemy import event


@contextmanager
def capture_sql(engine):
    statements = []

    def before_cursor_execute(
        connection, cursor, statement, parameters, context, executemany
    ):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


def assert_no_knowledge_sql(statements):
    assert statements, "capture must observe actual database work"
    for statement in statements:
        # Handle both quoted and unquoted schema identifiers, case-insensitively.
        assert not re.search(r'\bknowledge"?\s*\.', statement, re.IGNORECASE), statement
