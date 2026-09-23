"""Write path for orchestrator-declared factory goals.

Backs the ``monolith-agent-set-factory-goals`` MCP tool: the orchestrator
(main-loop Opus today, the factory conductor once its lane is enabled)
replaces the active goal set in one call. The old rows are deactivated, never
deleted, so declaration history survives. Validation lives in
``observability.factory_goals`` so the read and write sides share it.
"""

from __future__ import annotations

from sqlmodel import Session, select

from core.db import get_engine
from observability.factory_goals import FactoryGoal, validate_goals


def replace_goals(goals: list[dict], declared_by: str) -> dict:
    """Deactivate the current set and insert the validated replacement."""
    cleaned = validate_goals(goals, declared_by)
    engine = get_engine()
    with Session(engine) as session:
        current = list(
            session.exec(
                select(FactoryGoal).where(FactoryGoal.active == True)  # noqa: E712
            ).all()
        )
        for row in current:
            row.active = False
            session.add(row)
        inserted = []
        for goal in cleaned:
            row = FactoryGoal(
                statement=goal["statement"],
                issue_numbers=goal["issue_numbers"],
                declared_by=goal["declared_by"],
                active=True,
            )
            session.add(row)
            inserted.append(row)
        session.commit()
        for row in inserted:
            session.refresh(row)
        return {
            "ok": True,
            "goals": [
                {"id": row.id, "statement": row.statement} for row in inserted
            ],
            "retired": len(current),
        }
