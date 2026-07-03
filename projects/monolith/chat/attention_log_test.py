"""Tests for chat.attention_log: the attention-gate decision log (ADR 035).

DB-backed tests run against in-memory SQLite with the chat schema stripped,
mirroring chat.acl_test.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat import attention_log
from chat.models import AttentionDecision


@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with the chat schema stripped for SQLite compat."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


class TestLogDecision:
    def test_engage_is_always_logged(self, engine):
        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision(
                "c1", "m1", "engage", 0.9, directive_version=2, _rng=lambda: 0.99
            )
        with Session(engine) as session:
            rows = session.exec(select(AttentionDecision)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.channel_id == "c1"
        assert row.message_id == "m1"
        assert row.decision == "engage"
        assert row.confidence == 0.9
        assert row.directive_version == 2

    def test_ignore_logged_when_sampled_in(self, engine):
        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision("c1", "m1", "ignore", 0.1, _rng=lambda: 0.0)
        with Session(engine) as session:
            rows = session.exec(select(AttentionDecision)).all()
        assert len(rows) == 1
        assert rows[0].decision == "ignore"

    def test_ignore_skipped_when_sampled_out(self, engine):
        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision("c1", "m1", "ignore", 0.1, _rng=lambda: 0.9)
        with Session(engine) as session:
            rows = session.exec(select(AttentionDecision)).all()
        assert rows == []

    def test_ids_are_stringified(self, engine):
        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision(111, 222, "engage", 0.5, _rng=lambda: 0.0)
        with Session(engine) as session:
            row = session.exec(select(AttentionDecision)).one()
        assert row.channel_id == "111"
        assert row.message_id == "222"


class TestDecisionCheckConstraint:
    def test_rejects_invalid_decision(self, engine):
        with Session(engine) as session:
            session.add(
                AttentionDecision(channel_id="c1", message_id="m1", decision="bogus")
            )
            with pytest.raises(IntegrityError):
                session.commit()

    def test_accepts_engage_and_ignore(self, engine):
        with Session(engine) as session:
            session.add(
                AttentionDecision(channel_id="c1", message_id="m1", decision="engage")
            )
            session.add(
                AttentionDecision(channel_id="c1", message_id="m2", decision="ignore")
            )
            session.commit()  # must not raise
