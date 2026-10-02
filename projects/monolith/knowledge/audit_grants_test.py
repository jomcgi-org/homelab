"""Real-Postgres coverage for the agents-tier batched retrieval upsert."""

import pytest
from sqlalchemy import text
from sqlmodel import Session, create_engine

from knowledge.audit import record_retrievals


def test_agents_role_can_upsert_counters_but_not_write_audit_ledger(pg, monkeypatch):
    monkeypatch.setenv("KG_AUDIT_ENABLED", "true")
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE agents_writer"))
            record_retrievals(session, ["audit-grants-fact", "audit-grants-fact"])
            record_retrievals(session, ["audit-grants-fact"])
            count = session.execute(
                text(
                    "SELECT count FROM knowledge.note_retrievals WHERE note_id = 'audit-grants-fact'"
                )
            ).scalar_one()
            assert count == 2
            session.rollback()
        for statement in (
            "DELETE FROM knowledge.note_retrievals",
            "UPDATE knowledge.note_retrievals SET note_id = 'different'",
            "INSERT INTO knowledge.audit_process_issues (cause_key, state, marker) VALUES ('other', 'write_started', 'test')",
            "INSERT INTO knowledge.audit_runs (job_name, stream, prompt_version) VALUES ('test', 'scheduled', 'test')",
        ):
            with Session(engine) as session:
                session.execute(text("SET ROLE agents_writer"))
                with pytest.raises(Exception, match="permission denied"):
                    session.execute(text(statement))
                session.rollback()
    finally:
        engine.dispose()
