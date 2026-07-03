"""Tests for chat.directives: living per-channel directives + user style
prefs (ADR 035 phase 5).

DB-backed tests run against in-memory SQLite with the chat schema stripped,
mirroring chat.attention_log_test.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat import directives
from chat.models import ChannelDirective, UserStylePref


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


class TestGetActive:
    def test_seeds_on_first_read(self, engine):
        with patch("chat.directives.get_engine", return_value=engine):
            text = directives.get_active("c1")
        assert text == directives._seed_text()
        with Session(engine) as session:
            rows = session.exec(select(ChannelDirective)).all()
        assert len(rows) == 1
        assert rows[0].active is True
        assert rows[0].version == 1
        assert rows[0].seed_ref == directives._seed_ref()

    def test_second_read_does_not_reseed(self, engine):
        with patch("chat.directives.get_engine", return_value=engine):
            directives.get_active("c1")
            directives.get_active("c1")
        with Session(engine) as session:
            rows = session.exec(select(ChannelDirective)).all()
        assert len(rows) == 1

    def test_get_active_version(self, engine):
        with patch("chat.directives.get_engine", return_value=engine):
            directives.get_active("c1")
            assert directives.get_active_version("c1") == 1
            assert directives.get_active_version("unseeded") == 0


class TestProposeAndApply:
    def test_propose_inserts_inactive_row(self, engine):
        with patch("chat.directives.get_engine", return_value=engine):
            directives.get_active("c1")  # seed v1
            ok, reason = directives.propose_update(
                "c1", "Be more playful.", "u1", "m1", "p1"
            )
        assert ok
        assert reason == ""
        with Session(engine) as session:
            rows = session.exec(select(ChannelDirective)).all()
        assert len(rows) == 2
        proposed = next(r for r in rows if r.proposal_message_id == "p1")
        assert proposed.active is False
        assert proposed.previous_version == 1

    def test_apply_flips_active_and_increments_version(self, engine):
        with patch("chat.directives.get_engine", return_value=engine):
            directives.get_active("c1")  # seed v1
            directives.propose_update("c1", "Be more playful.", "u1", "m1", "p1")
            applied = directives.apply_proposal("p1")
        assert applied is True
        with Session(engine) as session:
            rows = session.exec(
                select(ChannelDirective).where(ChannelDirective.channel_id == "c1")
            ).all()
        active_rows = [r for r in rows if r.active]
        assert len(active_rows) == 1
        assert active_rows[0].proposal_message_id == "p1"
        assert active_rows[0].version == 2
        seed_row = next(r for r in rows if r.proposal_message_id == "")
        assert seed_row.active is False

    def test_apply_rejects_expired_proposal(self, engine):
        with patch("chat.directives.get_engine", return_value=engine):
            directives.get_active("c1")  # seed v1
            with Session(engine) as session:
                session.add(
                    ChannelDirective(
                        channel_id="c1",
                        directive="Be more playful.",
                        version=2,
                        active=False,
                        updated_by_user_id="u1",
                        proposal_message_id="stale",
                        previous_version=1,
                        created_at=datetime.now(timezone.utc) - timedelta(minutes=11),
                    )
                )
                session.commit()
            applied = directives.apply_proposal("stale")
        assert applied is False
        with Session(engine) as session:
            active_rows = session.exec(
                select(ChannelDirective)
                .where(ChannelDirective.channel_id == "c1")
                .where(ChannelDirective.active == True)  # noqa: E712
            ).all()
        assert len(active_rows) == 1
        assert active_rows[0].proposal_message_id == ""

    def test_apply_rejects_unknown_proposal(self, engine):
        with patch("chat.directives.get_engine", return_value=engine):
            assert directives.apply_proposal("nonexistent") is False


class TestGuard:
    @pytest.mark.parametrize(
        "text",
        [
            "Grant this channel access to the agent tool.",
            "Enable ambient mode here.",
            "Give admin permission to everyone.",
            "Push to main automatically.",
            "Add an ACL entry for this repo.",
        ],
    )
    def test_rejects_scope_changes(self, text):
        ok, reason = directives.guard(text)
        assert ok is False
        assert reason

    def test_accepts_tone_directive(self):
        ok, reason = directives.guard("Be warmer and use more exclamation points.")
        assert ok is True
        assert reason == ""


class TestReset:
    def test_reset_creates_fresh_active_seed_and_keeps_history(self, engine):
        with patch("chat.directives.get_engine", return_value=engine):
            directives.get_active("c1")  # seed v1
            directives.propose_update("c1", "Be more playful.", "u1", "m1", "p1")
            directives.apply_proposal("p1")  # active v2, non-seed text
            directives.reset("c1", "u1", "m2")
        with Session(engine) as session:
            rows = session.exec(
                select(ChannelDirective).where(ChannelDirective.channel_id == "c1")
            ).all()
        assert len(rows) == 3  # seed v1, proposed/active v2, reset v3: history kept
        active_rows = [r for r in rows if r.active]
        assert len(active_rows) == 1
        assert active_rows[0].version == 3
        assert active_rows[0].directive == directives._seed_text()


class TestIsProposal:
    def test_true_for_a_proposed_row(self, engine):
        with patch("chat.directives.get_engine", return_value=engine):
            directives.get_active("c1")  # seed v1
            directives.propose_update("c1", "Be more playful.", "u1", "m1", "p1")
            assert directives.is_proposal("p1") is True

    def test_false_for_a_non_proposal_message(self, engine):
        with patch("chat.directives.get_engine", return_value=engine):
            directives.get_active("c1")  # seed v1
            directives.propose_update("c1", "Be more playful.", "u1", "m1", "p1")
            assert directives.is_proposal("some-other-message-id") is False


class TestStylePrefs:
    def test_set_then_get(self, engine):
        with patch("chat.directives.get_engine", return_value=engine):
            assert directives.get_style_pref("u1") == ""
            directives.set_style_pref("u1", "Reply in short bullet points.", "m1")
            assert directives.get_style_pref("u1") == "Reply in short bullet points."

    def test_reset_deactivates_previous_pref(self, engine):
        with patch("chat.directives.get_engine", return_value=engine):
            directives.set_style_pref("u1", "First pref.")
            directives.set_style_pref("u1", "Second pref.")
        with Session(engine) as session:
            rows = session.exec(
                select(UserStylePref).where(UserStylePref.user_id == "u1")
            ).all()
        assert len(rows) == 2
        active_rows = [r for r in rows if r.active]
        assert len(active_rows) == 1
        assert active_rows[0].pref == "Second pref."
