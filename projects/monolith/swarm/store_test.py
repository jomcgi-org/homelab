import pytest
from sqlmodel import Session, SQLModel, create_engine

from swarm.store import (
    InvalidDecision,
    NoOpenDecision,
    expire_decision,
    get_open_decision,
    list_open_decisions,
    open_decision,
    record_decision,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'swarm-store.db'}")
    schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in schemas:
                table.schema = schemas[table.name]


def test_open_decision_is_idempotent(db):
    with Session(db) as session:
        first = open_decision(
            session,
            "wf-1",
            "push_gate",
            "push_gate",
            ["approve", "send_back"],
            "first note",
        )
        second = open_decision(
            session,
            "wf-1",
            "push_gate",
            "push_gate",
            ["approve"],
            "replacement note",
        )

        assert second.id == first.id
        assert second.options == ["approve", "send_back"]
        assert second.note == "first note"
        assert list_open_decisions(session, "wf-1") == [second]


def test_open_decision_recovers_from_concurrent_insert(db, monkeypatch):
    with Session(db) as winner, Session(db) as contender:
        contender_commit = contender.commit

        def commit_after_winner():
            open_decision(
                winner,
                "wf-race",
                "push_gate",
                "push_gate",
                ["approve", "send_back"],
                "winner",
            )
            contender_commit()

        monkeypatch.setattr(contender, "commit", commit_after_winner)

        recovered = open_decision(
            contender,
            "wf-race",
            "push_gate",
            "push_gate",
            ["approve", "send_back"],
            "contender",
        )

        assert recovered.note == "winner"
        assert recovered.id is not None
        assert list_open_decisions(contender, "wf-race") == [recovered]


def test_record_decision_is_idempotent_on_repeat(db):
    with Session(db) as session:
        open_decision(
            session,
            "wf-1",
            "push_gate",
            "push_gate",
            ["approve", "send_back"],
            None,
        )
        first = record_decision(
            session,
            "wf-1",
            "push_gate",
            "approve",
            "ship it",
            "joe@example.com",
            "cloudflare",
        )
        repeated = record_decision(
            session,
            "wf-1",
            "push_gate",
            "approve",
            "changed note",
            "someone@example.com",
            "other",
        )

        assert repeated.id == first.id
        assert repeated.decision_note == "ship it"
        assert repeated.actor_subject == "joe@example.com"
        assert get_open_decision(session, "wf-1", "push_gate") is None


def test_record_decision_rejects_invalid_option(db):
    with Session(db) as session:
        open_decision(
            session,
            "wf-1",
            "review",
            "review_escalation",
            ["retry", "send_back"],
            None,
        )

        with pytest.raises(InvalidDecision):
            record_decision(
                session,
                "wf-1",
                "review",
                "approve",
                None,
                "joe@example.com",
                "cloudflare",
            )


def test_record_decision_requires_open_row(db):
    with Session(db) as session:
        with pytest.raises(NoOpenDecision):
            record_decision(
                session,
                "wf-missing",
                "push_gate",
                "approve",
                None,
                "joe@example.com",
                "cloudflare",
            )


def test_expire_decision_closes_open_row(db):
    with Session(db) as session:
        opened = open_decision(
            session,
            "wf-1",
            "push_gate",
            "push_gate",
            ["approve", "send_back"],
            None,
        )
        expired = expire_decision(session, "wf-1", "push_gate")

        assert expired is not None
        assert expired.id == opened.id
        assert expired.decision == "expired"
        assert expired.decided_at is not None
        assert get_open_decision(session, "wf-1", "push_gate") is None
