"""Tests for chat.observer_job: the weekly directive-evolution observer.

The pure DB helpers run against a schema-stripped in-memory SQLite engine; the
async handler is driven with find_style_friction, the LLM caller, and the DB
phases faked, so no network or real engine is touched.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

import chat.observer as observer
import chat.observer_job as observer_job
from chat.models import ChannelDirective, DiscordFeatureGrant, Message

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with the chat schema stripped for SQLite compat."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original:
            table.schema = original[table.name]


def _grant(session, scope, *, feature="ambient", subject_id="", guild_id="g1"):
    session.add(
        DiscordFeatureGrant(
            guild_id=guild_id, subject_id=subject_id, feature=feature, scope=scope
        )
    )


def _message(
    session, mid, channel_id, is_bot, created_at, *, content="hi", user="Alice"
):
    session.add(
        Message(
            discord_message_id=mid,
            channel_id=channel_id,
            user_id="bot" if is_bot else "u1",
            username="Bot" if is_bot else user,
            content=content,
            is_bot=is_bot,
            embedding=[0.0] * 1024,
            created_at=created_at,
        )
    )


# --- env knob parsing -------------------------------------------------------


def test_int_env_defaults_when_missing(monkeypatch):
    monkeypatch.delenv("OBSERVER_MIN_EVIDENCE", raising=False)
    assert observer_job._int_env("OBSERVER_MIN_EVIDENCE", 3) == 3


def test_int_env_parses_valid(monkeypatch):
    monkeypatch.setenv("OBSERVER_MIN_EVIDENCE", "5")
    assert observer_job._int_env("OBSERVER_MIN_EVIDENCE", 3) == 5


def test_int_env_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("OBSERVER_COOLDOWN_DAYS", "not-a-number")
    assert observer_job._int_env("OBSERVER_COOLDOWN_DAYS", 14) == 14


def test_int_env_falls_back_on_non_positive(monkeypatch):
    monkeypatch.setenv("OBSERVER_COOLDOWN_DAYS", "0")
    assert observer_job._int_env("OBSERVER_COOLDOWN_DAYS", 14) == 14


# --- granted-channel enumeration -------------------------------------------


def test_granted_channel_ids_only_server_wide_ambient(engine):
    with Session(engine) as session:
        _grant(session, "chanA")
        _grant(session, "chanB")
        _grant(session, "chanA", guild_id="g2")  # same scope other guild, deduped
        _grant(session, "chanC", feature="agent")  # wrong feature, ignored
        _grant(session, "chanD", subject_id="u1")  # per-user ambient, ignored
        _grant(session, "")  # whole-feature ambient (no channel), ignored
        session.commit()
        assert observer_job._granted_channel_ids(session) == ["chanA", "chanB"]


# --- cooldown ---------------------------------------------------------------


def _directive(session, channel_id, created_at, *, proposal_id="p1"):
    session.add(
        ChannelDirective(
            channel_id=channel_id,
            directive="d",
            version=2,
            active=False,
            proposal_message_id=proposal_id,
            created_at=created_at,
        )
    )


def test_cooldown_active_within_window(engine):
    with Session(engine) as session:
        _directive(session, "chanA", NOW - timedelta(days=2))
        session.commit()
        assert observer_job._cooldown_active(session, "chanA", NOW, 14) is True


def test_cooldown_inactive_beyond_window(engine):
    with Session(engine) as session:
        _directive(session, "chanA", NOW - timedelta(days=30))
        session.commit()
        assert observer_job._cooldown_active(session, "chanA", NOW, 14) is False


def test_cooldown_ignores_seed_rows(engine):
    """A seeded/reset directive has no proposal_message_id, so it never counts as
    a recent proposal that would block the observer."""
    with Session(engine) as session:
        _directive(session, "chanA", NOW, proposal_id="")
        session.commit()
        assert observer_job._cooldown_active(session, "chanA", NOW, 14) is False


# --- exchange retrieval -----------------------------------------------------


def test_channel_exchanges_only_bot_adjacent_user_messages(engine):
    """Only non-bot messages immediately following a bot message become
    exchanges; a user message not preceded by the bot is dropped."""
    base = NOW - timedelta(hours=1)
    with Session(engine) as session:
        _message(
            session, "1", "chanA", is_bot=False, created_at=base
        )  # opener, no bot before
        _message(
            session, "2", "chanA", is_bot=True, created_at=base + timedelta(minutes=1)
        )
        _message(
            session,
            "3",
            "chanA",
            is_bot=False,
            created_at=base + timedelta(minutes=2),
            content="too long",
            user="Bob",
        )  # follows bot -> included
        _message(
            session, "4", "chanA", is_bot=False, created_at=base + timedelta(minutes=3)
        )  # follows a user -> excluded
        _message(
            session, "5", "chanA", is_bot=True, created_at=base + timedelta(minutes=4)
        )
        _message(
            session, "6", "chanA", is_bot=False, created_at=base + timedelta(minutes=5)
        )  # follows bot -> included
        session.commit()
        exchanges = observer_job._channel_exchanges(session, "chanA")
    assert [e["message_id"] for e in exchanges] == ["3", "6"]
    assert exchanges[0] == {"message_id": "3", "author": "Bob", "text": "too long"}


# --- candidate gathering (integration over the sync helpers) ----------------


def test_gather_candidates_filters_grants_cooldown_and_empty(engine):
    base = NOW - timedelta(hours=1)
    with Session(engine) as session:
        # chanA: granted, off cooldown, has a bot-adjacent user message -> candidate
        _grant(session, "chanA")
        _message(session, "a1", "chanA", is_bot=True, created_at=base)
        _message(
            session, "a2", "chanA", is_bot=False, created_at=base + timedelta(minutes=1)
        )
        # chanB: granted but in cooldown -> skipped
        _grant(session, "chanB")
        _directive(session, "chanB", NOW - timedelta(days=1))
        _message(session, "b1", "chanB", is_bot=True, created_at=base)
        _message(
            session, "b2", "chanB", is_bot=False, created_at=base + timedelta(minutes=1)
        )
        # chanC: granted, off cooldown, but no bot-adjacent user message -> skipped
        _grant(session, "chanC")
        _message(session, "c1", "chanC", is_bot=False, created_at=base)
        # chanD: has messages but no ambient grant -> never observed
        _message(session, "d1", "chanD", is_bot=True, created_at=base)
        _message(
            session, "d2", "chanD", is_bot=False, created_at=base + timedelta(minutes=1)
        )
        session.commit()

    with patch("core.db.get_engine", return_value=engine):
        candidates = observer_job._gather_candidates(NOW, 14)

    assert [c[0] for c in candidates] == ["chanA"]
    assert [e["message_id"] for e in candidates[0][1]] == ["a2"]


# --- proposal composition ---------------------------------------------------


def test_proposal_content_has_directive_evidence_and_react_line():
    exchanges = [
        {"message_id": "1", "author": "Bob", "text": "x" * 200},
        {"message_id": "2", "author": "Amy", "text": "way too wordy"},
    ]
    content = observer_job._proposal_content("reply concisely", ["1", "2"], exchanges)
    assert content.startswith("Proposed directive for this channel:\n> reply concisely")
    assert "Bob: " in content and content.count("...") >= 1  # long snippet truncated
    assert "Amy: way too wordy" in content
    assert content.endswith("React 👍 to apply or 👎 to discard.")


# --- handler orchestration --------------------------------------------------


@pytest.mark.asyncio
async def test_handler_proposes_once_per_finding_with_env_knob(monkeypatch):
    monkeypatch.setenv("OBSERVER_MIN_EVIDENCE", "5")
    exchanges = [{"message_id": "3", "author": "Bob", "text": "too long"}]
    finding = {"directive_change": "reply concisely", "evidence_message_ids": ["3"]}

    friction = AsyncMock(return_value=finding)
    with (
        patch.object(
            observer_job, "_gather_candidates", return_value=[("chanA", exchanges)]
        ),
        patch.object(observer, "find_style_friction", friction),
        patch("chat.summarizer.build_llm_caller", return_value=AsyncMock()),
        patch.object(observer_job, "_enqueue_proposal") as enqueue,
    ):
        await observer_job.observe_directives_handler(session=None)

    friction.assert_awaited_once()
    assert friction.await_args.kwargs["min_evidence"] == 5
    enqueue.assert_called_once_with("chanA", finding, exchanges)


@pytest.mark.asyncio
async def test_handler_no_finding_enqueues_nothing():
    exchanges = [{"message_id": "3", "author": "Bob", "text": "one-off complaint"}]
    with (
        patch.object(
            observer_job, "_gather_candidates", return_value=[("chanA", exchanges)]
        ),
        patch.object(observer, "find_style_friction", AsyncMock(return_value=None)),
        patch("chat.summarizer.build_llm_caller", return_value=AsyncMock()),
        patch.object(observer_job, "_enqueue_proposal") as enqueue,
    ):
        await observer_job.observe_directives_handler(session=None)

    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_handler_no_candidates_short_circuits():
    with (
        patch.object(observer_job, "_gather_candidates", return_value=[]),
        patch("chat.summarizer.build_llm_caller") as caller,
        patch.object(observer, "find_style_friction") as friction,
    ):
        await observer_job.observe_directives_handler(session=None)

    caller.assert_not_called()
    friction.assert_not_called()
