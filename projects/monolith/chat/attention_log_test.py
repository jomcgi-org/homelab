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


class TestSetReplyMessage:
    def test_updates_latest_engage_row(self, engine):
        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision("c1", "m1", "engage", 0.9, _rng=lambda: 0.0)
            attention_log.set_reply_message("c1", "m1", "reply1")
        with Session(engine) as session:
            row = session.exec(select(AttentionDecision)).one()
        assert row.reply_message_id == "reply1"

    def test_picks_newest_engage_for_trigger(self, engine):
        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision("c1", "m1", "engage", 0.9, _rng=lambda: 0.0)
            attention_log.log_decision("c1", "m1", "engage", 0.9, _rng=lambda: 0.0)
            attention_log.set_reply_message("c1", "m1", "reply1")
        with Session(engine) as session:
            rows = session.exec(
                select(AttentionDecision).order_by(AttentionDecision.id)
            ).all()
        # Only the newest engage row (highest id) is linked.
        assert rows[0].reply_message_id is None
        assert rows[1].reply_message_id == "reply1"

    def test_noop_when_no_engage_row(self, engine):
        with patch("chat.attention_log.get_engine", return_value=engine):
            # An ignore row exists for the trigger, but no engage: no-op.
            attention_log.log_decision("c1", "m1", "ignore", 0.1, _rng=lambda: 0.0)
            attention_log.set_reply_message("c1", "m1", "reply1")
        with Session(engine) as session:
            row = session.exec(select(AttentionDecision)).one()
        assert row.reply_message_id is None

    def test_updates_only_matching_channel_and_trigger(self, engine):
        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision("c1", "m1", "engage", 0.9, _rng=lambda: 0.0)
            attention_log.log_decision("c2", "m1", "engage", 0.9, _rng=lambda: 0.0)
            attention_log.log_decision("c1", "m2", "engage", 0.9, _rng=lambda: 0.0)
            attention_log.set_reply_message("c1", "m1", "reply1")
        with Session(engine) as session:
            rows = {
                (r.channel_id, r.message_id): r.reply_message_id
                for r in session.exec(select(AttentionDecision)).all()
            }
        assert rows[("c1", "m1")] == "reply1"
        assert rows[("c2", "m1")] is None
        assert rows[("c1", "m2")] is None

    def test_ids_are_stringified(self, engine):
        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision(111, 222, "engage", 0.9, _rng=lambda: 0.0)
            attention_log.set_reply_message(111, 222, 333)
        with Session(engine) as session:
            row = session.exec(select(AttentionDecision)).one()
        assert row.reply_message_id == "333"


class TestSetWithheldReason:
    def test_records_reason_on_latest_engage(self, engine):
        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision("c1", "m1", "engage", 0.9, _rng=lambda: 0.0)
            attention_log.set_withheld_reason(
                "c1", "m1", attention_log.WITHHELD_SEND_GATE
            )
        with Session(engine) as session:
            row = session.exec(select(AttentionDecision)).one()
        assert row.withheld_reason == "send_gate"
        # A withheld engage never has a reply id.
        assert row.reply_message_id is None

    def test_picks_newest_engage_for_trigger(self, engine):
        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision("c1", "m1", "engage", 0.9, _rng=lambda: 0.0)
            attention_log.log_decision("c1", "m1", "engage", 0.9, _rng=lambda: 0.0)
            attention_log.set_withheld_reason(
                "c1", "m1", attention_log.WITHHELD_NO_REPLY
            )
        with Session(engine) as session:
            rows = session.exec(
                select(AttentionDecision).order_by(AttentionDecision.id)
            ).all()
        assert rows[0].withheld_reason is None
        assert rows[1].withheld_reason == "no_reply"

    def test_noop_when_no_engage_row(self, engine):
        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision("c1", "m1", "ignore", 0.1, _rng=lambda: 0.0)
            attention_log.set_withheld_reason(
                "c1", "m1", attention_log.WITHHELD_AGENT_THREAD
            )
        with Session(engine) as session:
            row = session.exec(select(AttentionDecision)).one()
        assert row.withheld_reason is None

    def test_ids_are_stringified(self, engine):
        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision(111, 222, "engage", 0.9, _rng=lambda: 0.0)
            attention_log.set_withheld_reason(111, 222, "empty_reply")
        with Session(engine) as session:
            row = session.exec(select(AttentionDecision)).one()
        assert row.withheld_reason == "empty_reply"


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
