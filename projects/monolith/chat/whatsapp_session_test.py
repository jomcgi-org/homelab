"""Tests for chat.whatsapp_session (ADR 039 Phase 4).

Covers session keying (sanitized, id-guard-safe, one row per group), the
household tool ACL, steering with author attribution, the reaction lifecycle
emitters, and the checklist repost when the WhatsApp edit window closes.

All DB-backed via in-memory SQLite (schema stripped like chat.store_test). These
are the pure session-logic seams; the gateway drain is tested on the Go side.
"""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat import goosecracker as gc
from chat import whatsapp_session
from chat.models import GoosecrackerSession, GoosecrackerSteering, WhatsappOutbox

_GROUP = "12345-67890@g.us"
_KEY = "wa-12345-67890-g-us"


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    originals = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            originals[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(whatsapp_session, "get_engine", lambda: engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in originals:
            table.schema = originals[table.name]


# --- Session keying ---------------------------------------------------------


class TestSessionKey:
    def test_sanitizes_disallowed_chars(self):
        assert whatsapp_session.wa_session_key(_GROUP) == _KEY

    def test_key_is_id_guard_safe(self):
        import re

        key = whatsapp_session.wa_session_key(_GROUP)
        # Same guard the internal progress/steering endpoints apply.
        assert re.match(r"^[A-Za-z0-9_-]{1,64}$", key)

    def test_is_whatsapp_session_key(self):
        assert whatsapp_session.is_whatsapp_session_key(_KEY)
        # A Discord thread id is a numeric snowflake; never wa-prefixed.
        assert not whatsapp_session.is_whatsapp_session_key("1234567890")


# --- Household ACL ----------------------------------------------------------


class TestHouseholdAcl:
    def test_repo_cluster_denied(self):
        # Only the credentialed families stay denied (ADR 039, amended): a
        # GitHub token / kubeconfig must not reach the partner-phone guest.
        assert whatsapp_session.household_allows("repo") is False
        assert whatsapp_session.household_allows("cluster") is False

    def test_local_capabilities_allowed(self):
        # Household gets every LOCAL capability, including artifact/chart builds.
        assert whatsapp_session.household_allows("knowledge") is True
        assert whatsapp_session.household_allows("calendar") is True
        assert whatsapp_session.household_allows("reminders") is True
        assert whatsapp_session.household_allows("artifact") is True


# --- Steering ---------------------------------------------------------------


def _seed_running(engine, key=_KEY):
    with Session(engine) as session:
        session.add(
            GoosecrackerSession(
                discord_thread=key,
                provider="whatsapp",
                provider_group_jid=_GROUP,
                recipe="agent",
                tier="household",
                running=True,
                running_since=datetime.now(timezone.utc),
                runner_instance=gc.INSTANCE_TOKEN,
            )
        )
        session.commit()


class TestSteering:
    def test_running_session_steers_with_attribution(self, engine):
        _seed_running(engine)
        steered = whatsapp_session.steer_or_none(
            _KEY,
            message_id="M9",
            sender_jid="alice@wa",
            sender_name="Alice",
            text="  actually make it blue  ",
        )
        assert steered is True
        with Session(engine) as session:
            row = session.exec(select(GoosecrackerSteering)).one()
            assert row.author_id == "alice@wa"
            assert row.author_name == "Alice"
            assert row.text == "actually make it blue"
            assert row.delivered is False
            react = session.exec(
                select(WhatsappOutbox).where(WhatsappOutbox.kind == "reaction")
            ).one()
        assert react.target_message_id == "M9"
        assert react.target_sender_jid == "alice@wa"
        assert react.reaction == gc.REACTION_RUNNING

    def test_no_session_returns_false(self, engine):
        assert (
            whatsapp_session.steer_or_none(
                _KEY, message_id="M1", sender_jid="a@wa", sender_name="A", text="hi"
            )
            is False
        )

    def test_idle_session_returns_false(self, engine):
        with Session(engine) as session:
            session.add(
                GoosecrackerSession(
                    discord_thread=_KEY,
                    provider="whatsapp",
                    provider_group_jid=_GROUP,
                    running=False,
                )
            )
            session.commit()
        assert (
            whatsapp_session.steer_or_none(
                _KEY, message_id="M1", sender_jid="a@wa", sender_name="A", text="hi"
            )
            is False
        )

    def test_blank_text_returns_false(self, engine):
        _seed_running(engine)
        assert (
            whatsapp_session.steer_or_none(
                _KEY, message_id="M1", sender_jid="a@wa", sender_name="A", text="   "
            )
            is False
        )


# --- Reaction lifecycle -----------------------------------------------------


def _row_with_trigger():
    return GoosecrackerSession(
        discord_thread=_KEY,
        provider="whatsapp",
        provider_group_jid=_GROUP,
        provider_trigger_message_id="M1",
        provider_trigger_sender_jid="alice@wa",
    )


class TestReactionLifecycle:
    def test_emit_running_reaction(self, engine):
        with Session(engine) as session:
            whatsapp_session.emit_running_reaction(session, _row_with_trigger())
            session.commit()
            rows = session.exec(select(WhatsappOutbox)).all()
        # ⏳ removed, 👀 added, both on the trigger.
        assert [(r.reaction, r.reaction_remove) for r in rows] == [
            (gc.REACTION_QUEUED, True),
            (gc.REACTION_RUNNING, False),
        ]
        assert all(
            r.target_message_id == "M1" and r.target_sender_jid == "alice@wa"
            for r in rows
        )

    def test_emit_terminal_reaction_success(self, engine):
        with Session(engine) as session:
            whatsapp_session.emit_terminal_reaction(session, _row_with_trigger(), True)
            session.commit()
            rows = session.exec(select(WhatsappOutbox)).all()
        pairs = [(r.reaction, r.reaction_remove) for r in rows]
        assert (gc.REACTION_QUEUED, True) in pairs
        assert (gc.REACTION_RUNNING, True) in pairs
        assert (gc.REACTION_DONE, False) in pairs

    def test_emit_terminal_reaction_failure(self, engine):
        with Session(engine) as session:
            whatsapp_session.emit_terminal_reaction(session, _row_with_trigger(), False)
            session.commit()
            rows = session.exec(select(WhatsappOutbox)).all()
        assert (gc.REACTION_FAILED, False) in [
            (r.reaction, r.reaction_remove) for r in rows
        ]

    def test_no_trigger_is_noop(self, engine):
        row = GoosecrackerSession(
            discord_thread=_KEY, provider="whatsapp", provider_group_jid=_GROUP
        )
        with Session(engine) as session:
            whatsapp_session.emit_running_reaction(session, row)
            session.commit()
            assert session.exec(select(WhatsappOutbox)).all() == []


# --- Checklist repost on edit-window expiry ---------------------------------


def _seed_checklist(engine, key=_KEY):
    """Seed a session with a checklist message and return that message's id."""
    with Session(engine) as session:
        base = WhatsappOutbox(group_jid=_GROUP, kind="message", content="planning")
        session.add(base)
        session.flush()
        base_id = base.id
        session.add(
            GoosecrackerSession(
                discord_thread=key,
                provider="whatsapp",
                provider_group_jid=_GROUP,
                checklist_outbox_id=base_id,
            )
        )
        session.commit()
    return base_id


class TestChecklistRepost:
    def test_repost_when_edit_window_expired(self, engine):
        base_id = _seed_checklist(engine)
        # The gateway consumed an edit as window-expired.
        with Session(engine) as session:
            session.add(
                WhatsappOutbox(
                    group_jid=_GROUP,
                    kind="edit",
                    content="v2",
                    edit_of=base_id,
                    last_error="edit_window_expired",
                    posted_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

        assert whatsapp_session._emit_checklist_edit(_KEY, "v3 checklist") is True

        with Session(engine) as session:
            row = session.get(GoosecrackerSession, _KEY)
            # Repointed to a fresh message carrying the new content.
            assert row.checklist_outbox_id != base_id
            fresh = session.get(WhatsappOutbox, row.checklist_outbox_id)
            assert fresh.kind == "message"
            assert fresh.content == "v3 checklist"
            # No NEW edit row this round (the fresh message already has it).
            edits = session.exec(
                select(WhatsappOutbox).where(WhatsappOutbox.kind == "edit")
            ).all()
            assert len(edits) == 1

    def test_normal_edit_when_window_open(self, engine):
        base_id = _seed_checklist(engine)
        assert whatsapp_session._emit_checklist_edit(_KEY, "updated") is True
        with Session(engine) as session:
            row = session.get(GoosecrackerSession, _KEY)
            assert row.checklist_outbox_id == base_id  # unchanged
            edit = session.exec(
                select(WhatsappOutbox).where(WhatsappOutbox.kind == "edit")
            ).one()
        assert edit.edit_of == base_id
        assert edit.content == "updated"

    def test_no_checklist_is_noop(self, engine):
        with Session(engine) as session:
            session.add(
                GoosecrackerSession(
                    discord_thread=_KEY,
                    provider="whatsapp",
                    provider_group_jid=_GROUP,
                )
            )
            session.commit()
        assert whatsapp_session._emit_checklist_edit(_KEY, "x") is False
