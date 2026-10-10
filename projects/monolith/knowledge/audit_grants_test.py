"""Real-Postgres coverage for the agents-tier batched retrieval upsert."""

import pytest
from sqlalchemy import text
from sqlmodel import Session, create_engine

from knowledge.audit import record_retrievals
from knowledge.models import Dispute, Note


def test_agents_dispute_can_fence_reviews_but_cannot_edit_claims_or_deadlines(pg):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            note = Note(
                note_id="review-dispute-grants-fact",
                path="review-dispute-grants-fact.md",
                title="Dispute revision permission fixture",
                content_hash="fixture-hash",
            )
            session.add(note)
            session.flush()
            note_id, revision = note.note_id, note.revision
            session.execute(text("SET ROLE agents_writer"))
            session.add(Dispute(note_id=note_id, reason="Review evidence is disputed"))
            session.flush()
            observed = session.execute(
                text("SELECT revision FROM knowledge.notes WHERE note_id = :note_id"),
                {"note_id": note_id},
            ).scalar_one()
            assert observed == revision + 1
            session.rollback()
        for statement in (
            "UPDATE knowledge.notes SET title = title WHERE false",
            "UPDATE knowledge.notes SET content_hash = content_hash WHERE false",
            "UPDATE knowledge.notes SET review_after = review_after WHERE false",
            "UPDATE knowledge.notes SET last_reviewed_at = last_reviewed_at WHERE false",
            "DELETE FROM knowledge.notes WHERE false",
        ):
            with Session(engine) as session:
                session.execute(text("SET ROLE agents_writer"))
                with pytest.raises(Exception, match="permission denied"):
                    session.execute(text(statement))
                session.rollback()
    finally:
        engine.dispose()


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
