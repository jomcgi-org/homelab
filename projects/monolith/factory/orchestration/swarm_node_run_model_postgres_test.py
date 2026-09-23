"""PostgreSQL coverage for the swarm_node_run.model backfill.

The migration casts the TEXT pin to jsonb, which SQLite cannot exercise, so the
backfill statement is replayed here against rows written before the column
existed.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from shared.testing.plugin import _find_migrations_dir

MIGRATION = "20260923140000_swarm_node_run_model.sql"


def _backfill() -> str:
    source = (_find_migrations_dir() / MIGRATION).read_text()
    statements = [
        "\n".join(
            line for line in chunk.splitlines() if not line.strip().startswith("--")
        ).strip()
        for chunk in source.split(";")
    ]
    return next(s for s in statements if s.upper().startswith("UPDATE"))


def test_the_backfill_copies_the_pinned_model_into_the_column(session):
    session.execute(
        text(
            "INSERT INTO swarm.swarm_task (id, task_text, conductor_model) "
            "VALUES ('model-backfill', 'task', 'opus')"
        )
    )
    rows = [
        ("implement_a", 1, json.dumps({"model": "spark", "attempt": 1})),
        ("implement_a", 2, json.dumps({"model": "sol", "escalated_from": "spark"})),
        ("implement_b", 1, None),
    ]
    for node_key, attempt, pin in rows:
        session.execute(
            text(
                "INSERT INTO swarm.swarm_node_run (task_id, node_key, attempt, "
                "pin_json, status) VALUES ('model-backfill', "
                ":node_key, :attempt, :pin, 'failed')"
            ),
            {"node_key": node_key, "attempt": attempt, "pin": pin},
        )
    session.execute(text(_backfill()))
    stored = session.execute(
        text(
            "SELECT node_key, attempt, model FROM swarm.swarm_node_run "
            "WHERE task_id = 'model-backfill' ORDER BY node_key, attempt"
        )
    ).all()
    assert [tuple(row) for row in stored] == [
        ("implement_a", 1, "spark"),
        ("implement_a", 2, "sol"),
        ("implement_b", 1, None),
    ]
