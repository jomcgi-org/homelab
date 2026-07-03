"""Tests for chat.whatsapp_session (ADR 039 Phase 4).

Covers session keying (sanitized, id-guard-safe, one row per group), the
household tool ACL at dispatch (a repo action is refused), the ⏳ reaction +
checklist enqueue at dispatch, steering with author attribution, the reaction
lifecycle emitters, and the checklist repost when the WhatsApp edit window closes.

All DB-backed via in-memory SQLite (schema stripped like chat.store_test), with
``goosecracker.api.submit`` patched so no real fc-invoke run is fired. These are
the pure session-logic seams; the gateway drain is tested on the Go side.
"""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import goosecracker.api as goose_api
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


def _no_dispatch(monkeypatch):
    """Patch submit to a no-op that records its kwargs, so dispatch fires no run."""
    calls = []

    def _fake(task, **kwargs):
        calls.append({"task": task, **kwargs})
        return {"session": kwargs["session"], "thread_id": "t-x", "action": "create"}

    monkeypatch.setattr(goose_api, "submit", _fake)
    return calls


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
    def test_repo_cluster_artifact_denied(self):
        assert whatsapp_session.household_allows("repo") is False
        assert whatsapp_session.household_allows("cluster") is False
        assert whatsapp_session.household_allows("artifact") is False

    def test_knowledge_calendar_reminders_allowed(self):
        assert whatsapp_session.household_allows("knowledge") is True
        assert whatsapp_session.household_allows("calendar") is True
        assert whatsapp_session.household_allows("reminders") is True


# --- Dispatch ---------------------------------------------------------------


class TestDispatch:
    def test_repo_action_refused(self, engine, monkeypatch):
        calls = _no_dispatch(monkeypatch)
        out = whatsapp_session.dispatch_whatsapp_agent(
            _GROUP,
            "open a PR on the repo",
            trigger_message_id="M1",
            trigger_sender_jid="alice@wa",
            repo="jomcgi/homelab",
        )
        assert out["action"] == "refused"
        # Nothing dispatched, and no session row created.
        assert calls == []
        with Session(engine) as session:
            assert session.get(GoosecrackerSession, _KEY) is None
            rows = session.exec(select(WhatsappOutbox)).all()
        # A one-line refusal is enqueued so the group sees it.
        assert len(rows) == 1
        assert rows[0].kind == "message"
        assert "repositories" in rows[0].content

    def test_dispatch_creates_session_and_ux(self, engine, monkeypatch):
        calls = _no_dispatch(monkeypatch)
        out = whatsapp_session.dispatch_whatsapp_agent(
            _GROUP,
            "plan the weekend",
            trigger_message_id="M1",
            trigger_sender_jid="alice@wa",
        )
        assert out["action"] == "dispatched"
        # submit routed through the whatsapp provider under the household tier,
        # keyed on the sanitized session id.
        assert calls[0]["provider"] == "whatsapp"
        assert calls[0]["tier"] == "household"
        assert calls[0]["recipe"] == "agent"
        assert calls[0]["session"] == _KEY
        assert calls[0]["discord_thread"] == _KEY

        with Session(engine) as session:
            row = session.get(GoosecrackerSession, _KEY)
            assert row.provider == "whatsapp"
            assert row.provider_group_jid == _GROUP
            assert row.provider_trigger_message_id == "M1"
            assert row.provider_trigger_sender_jid == "alice@wa"
            assert row.tier == "household"
            assert row.running is True
            assert row.runner_instance == gc.INSTANCE_TOKEN
            assert row.checklist_outbox_id is not None
            outbox = session.exec(select(WhatsappOutbox)).all()

        # Exactly a ⏳ reaction on the trigger and one checklist message.
        assert sorted(r.kind for r in outbox) == ["message", "reaction"]
        react = next(r for r in outbox if r.kind == "reaction")
        assert react.target_message_id == "M1"
        assert react.target_sender_jid == "alice@wa"
        assert react.reaction == gc.REACTION_QUEUED
        assert react.reaction_remove is False
        msg = next(r for r in outbox if r.kind == "message")
        assert msg.id == row.checklist_outbox_id

    def test_one_active_session_per_group(self, engine, monkeypatch):
        _no_dispatch(monkeypatch)
        whatsapp_session.dispatch_whatsapp_agent(
            _GROUP, "first", trigger_message_id="M1", trigger_sender_jid="a@wa"
        )
        whatsapp_session.dispatch_whatsapp_agent(
            _GROUP, "second", trigger_message_id="M2", trigger_sender_jid="b@wa"
        )
        with Session(engine) as session:
            rows = session.exec(
                select(GoosecrackerSession).where(
                    GoosecrackerSession.discord_thread == _KEY
                )
            ).all()
        # PK derived from the group, so re-dispatch reuses the single row.
        assert len(rows) == 1
        assert rows[0].provider_trigger_message_id == "M2"


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
