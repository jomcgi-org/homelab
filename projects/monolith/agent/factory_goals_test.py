from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import agent.factory_goals as agent_factory_goals
from agent.factory_goals import replace_goals
from observability.factory_goals import FactoryGoal


@pytest.fixture(name="engine")
def engine_fixture(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'agent-factory-goals.db'}")
    table = FactoryGoal.__table__
    original_schema = table.schema
    table.schema = None
    try:
        SQLModel.metadata.create_all(engine, tables=[table])
        # replace_goals opens its own Session(get_engine()); point it here.
        monkeypatch.setattr(agent_factory_goals, "get_engine", lambda: engine)
        yield engine
    finally:
        table.schema = original_schema
        engine.dispose()


def _all_rows(engine):
    with Session(engine) as session:
        return list(session.exec(select(FactoryGoal)).all())


def test_replace_goals_inserts_the_first_active_set(engine):
    result = replace_goals(
        [{"statement": "Ship goals", "issue_numbers": [5927]}], "opus"
    )

    assert result["retired"] == 0
    assert [row["statement"] for row in result["goals"]] == ["Ship goals"]
    rows = _all_rows(engine)
    assert len(rows) == 1
    assert rows[0].active is True
    assert rows[0].statement == "Ship goals"


def test_replace_goals_retires_prior_set_and_preserves_history(engine):
    replace_goals([{"statement": "First goal", "issue_numbers": [5927]}], "opus")

    result = replace_goals(
        [
            {"statement": "Second goal", "issue_numbers": [5927]},
            {"statement": "Third goal", "issue_numbers": [5784]},
        ],
        "opus",
    )

    assert result["retired"] == 1
    assert [row["statement"] for row in result["goals"]] == [
        "Second goal",
        "Third goal",
    ]
    rows = _all_rows(engine)
    assert len(rows) == 3
    by_statement = {row.statement: row for row in rows}
    assert by_statement["First goal"].active is False
    assert by_statement["Second goal"].active is True
    assert by_statement["Third goal"].active is True


def test_replace_goals_rejects_invalid_input_without_writing(engine):
    with pytest.raises(ValueError):
        replace_goals([], "opus")

    assert _all_rows(engine) == []
