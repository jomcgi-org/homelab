"""Reusable SQL capture for proving campaign extraction stays out of knowledge."""

import re
from contextlib import contextmanager

from sqlalchemy import event
from sqlmodel import SQLModel


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
    # sqlite_harness removes schemas from Table objects for create_all, but
    # metadata retains the original qualified keys. Check those flattened
    # names too so an accidental ORM knowledge write cannot evade this guard.
    knowledge_tables = {
        table.name.casefold()
        for key, table in SQLModel.metadata.tables.items()
        if key.startswith("knowledge.")
    }
    for statement in statements:
        # Handle both quoted and unquoted schema identifiers, case-insensitively.
        assert not re.search(r'\bknowledge"?\s*\.', statement, re.IGNORECASE), statement
        for table in re.findall(
            r'\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+"?([a-z_][a-z_0-9]*)"?(?![a-z_0-9]|\s*\.)',
            statement,
            re.IGNORECASE,
        ):
            assert table.casefold() not in knowledge_tables, statement
