"""Tests for chat.ambient_analysis: the deterministic scoring core plus the
injected-caller classifier (ADR chat/007, PR 3).

DB-backed tests run against in-memory SQLite with the chat schema stripped,
mirroring chat.directives_test. Datetimes are naive (SQLite has no tz-aware
type); the scoring core never mixes naive and aware within a store, so the math
holds under both SQLite (tests) and Postgres (prod).
"""

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from chat import ambient_analysis
from chat.models import AttentionDecision, Message, ReactionEvent

T0 = datetime(2026, 7, 1, 12, 0)
THUMBSUP = "\U0001f44d"
THUMBSDOWN = "\U0001f44e"


@pytest.fixture(name="engine")
def engine_fixture():
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


def _engage(session, channel_id, message_id, reply_id, at, *, author=None):
    session.add(
        AttentionDecision(
            channel_id=channel_id,
            message_id=message_id,
            decision="engage",
            reply_message_id=reply_id,
            created_at=at,
        )
    )
    if author is not None:
        session.add(
            Message(
                discord_message_id=message_id,
                channel_id=channel_id,
                user_id=author,
                username=author,
                content="trigger text",
                is_bot=False,
                embedding=[0.0] * 1024,
                created_at=at,
            )
        )


def _reaction(session, channel_id, message_id, emoji, at, action="add"):
    session.add(
        ReactionEvent(
            channel_id=channel_id,
            message_id=message_id,
            emoji=emoji,
            action=action,
            reactor_id="human",
            created_at=at,
        )
    )


def _followup(session, channel_id, at, discord_id="f1", author="u9"):
    session.add(
        Message(
            discord_message_id=discord_id,
            channel_id=channel_id,
            user_id=author,
            username=author,
            content="a human reply",
            is_bot=False,
            embedding=[0.0] * 1024,
            created_at=at,
        )
    )


class TestScoreWindow:
    def test_positive_reaction_only(self, engine):
        with Session(engine) as s:
            _engage(s, "c1", "m1", "r1", T0, author="u1")
            _reaction(s, "c1", "r1", THUMBSUP, T0 + timedelta(minutes=5))
            s.commit()
            # valence +1, no followup, not barged (valence > 0) -> 0.5 * 1 = 0.5
            score = ambient_analysis.score_window(
                s, "channel", "c1", T0 - timedelta(minutes=1), T0 + timedelta(hours=1)
            )
        assert score == pytest.approx(0.5)

    def test_negative_reaction_and_barge_in(self, engine):
        with Session(engine) as s:
            _engage(s, "c2", "m2", "r2", T0, author="u2")
            _reaction(s, "c2", "r2", THUMBSDOWN, T0 + timedelta(minutes=5))
            s.commit()
            # valence -1, no followup -> barged_in=1 -> 0.5*-1 - 0.2 = -0.7
            score = ambient_analysis.score_window(
                s, "channel", "c2", T0 - timedelta(minutes=1), T0 + timedelta(hours=1)
            )
        assert score == pytest.approx(-0.7)

    def test_followup_adds_component(self, engine):
        with Session(engine) as s:
            _engage(s, "c3", "m3", "r3", T0, author="u3")
            _reaction(s, "c3", "r3", THUMBSUP, T0 + timedelta(minutes=2))
            _followup(s, "c3", T0 + timedelta(minutes=3), discord_id="f3")
            s.commit()
            # valence +1, followup 1, not barged -> 0.5 + 0.3 = 0.8
            score = ambient_analysis.score_window(
                s, "channel", "c3", T0 - timedelta(minutes=1), T0 + timedelta(hours=1)
            )
        assert score == pytest.approx(0.8)

    def test_none_when_no_episodes(self, engine):
        with Session(engine) as s:
            score = ambient_analysis.score_window(
                s, "channel", "empty", T0, T0 + timedelta(hours=1)
            )
        assert score is None

    def test_user_scope_filters_by_trigger_author(self, engine):
        with Session(engine) as s:
            _engage(s, "c1", "m1", "r1", T0, author="alice")
            _reaction(s, "c1", "r1", THUMBSUP, T0 + timedelta(minutes=2))
            _engage(s, "c1", "m2", "r2", T0, author="bob")
            _reaction(s, "c1", "r2", THUMBSDOWN, T0 + timedelta(minutes=2))
            s.commit()
            alice = ambient_analysis.score_window(
                s, "user", "alice", T0 - timedelta(minutes=1), T0 + timedelta(hours=1)
            )
            bob = ambient_analysis.score_window(
                s, "user", "bob", T0 - timedelta(minutes=1), T0 + timedelta(hours=1)
            )
        assert alice == pytest.approx(0.5)
        assert bob == pytest.approx(-0.7)

    def test_reaction_window_fallback_when_no_reply_id(self, engine):
        with Session(engine) as s:
            _engage(s, "c4", "m4", None, T0, author="u4")
            # No reply id: a reaction anywhere in the channel within the window
            # is attributed by the fallback.
            _reaction(s, "c4", "someothermsg", THUMBSUP, T0 + timedelta(minutes=3))
            s.commit()
            score = ambient_analysis.score_window(
                s, "channel", "c4", T0 - timedelta(minutes=1), T0 + timedelta(hours=1)
            )
        assert score == pytest.approx(0.5)

    def test_reaction_remove_cancels_add(self, engine):
        with Session(engine) as s:
            _engage(s, "c5", "m5", "r5", T0, author="u5")
            _reaction(s, "c5", "r5", THUMBSUP, T0 + timedelta(minutes=1))
            _reaction(
                s, "c5", "r5", THUMBSUP, T0 + timedelta(minutes=2), action="remove"
            )
            s.commit()
            # valence nets to 0, no followup -> barged_in=1 -> -0.2
            score = ambient_analysis.score_window(
                s, "channel", "c5", T0 - timedelta(minutes=1), T0 + timedelta(hours=1)
            )
        assert score == pytest.approx(-0.2)


class TestGatherScopeEpisodes:
    def test_clusters_by_channel_and_author(self, engine):
        with Session(engine) as s:
            _engage(s, "c1", "m1", "r1", T0, author="alice")
            _reaction(s, "c1", "r1", THUMBSUP, T0 + timedelta(minutes=1))
            _engage(s, "c1", "m2", "r2", T0 + timedelta(minutes=10), author="alice")
            s.commit()
            clusters = ambient_analysis.gather_scope_episodes(s, T0 - timedelta(days=1))
        assert set(clusters["channel"]["c1"][0]) >= {
            "episode_id",
            "reaction_valence",
            "followup",
            "barged_in",
            "score",
        }
        assert len(clusters["channel"]["c1"]) == 2
        assert len(clusters["user"]["alice"]) == 2
        assert clusters["channel"]["c1"][0]["reaction_valence"] == 1


class _FakeCaller:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    async def __call__(self, prompt):
        self.calls += 1
        return self.reply


class TestClassifyScope:
    @pytest.mark.asyncio
    async def test_propose_path(self):
        caller = _FakeCaller(
            '{"propose": true, "proposed_text": "Reply more concisely.", '
            '"confidence": 0.9, "evidence_ids": [1, 2], "rationale": "too long"}'
        )
        episodes = [
            {
                "episode_id": 1,
                "reaction_valence": -1,
                "followup": 0,
                "barged_in": 1,
                "text": "too wordy",
            },
            {
                "episode_id": 2,
                "reaction_valence": 0,
                "followup": 0,
                "barged_in": 1,
                "text": "again wordy",
            },
        ]
        result = await ambient_analysis.classify_scope(
            caller, "channel", "c1", episodes, "current directive"
        )
        assert result["proposed_text"] == "Reply more concisely."
        assert result["confidence"] == pytest.approx(0.9)
        assert result["evidence_ids"] == [1, 2]

    @pytest.mark.asyncio
    async def test_abstain_path(self):
        caller = _FakeCaller('{"propose": false, "confidence": 0}')
        episodes = [
            {
                "episode_id": 1,
                "reaction_valence": 1,
                "followup": 1,
                "barged_in": 0,
                "text": "fine",
            }
        ]
        result = await ambient_analysis.classify_scope(
            caller, "channel", "c1", episodes, "current"
        )
        assert result["confidence"] == 0.0
        assert result["proposed_text"] == ""

    @pytest.mark.asyncio
    async def test_hallucinated_evidence_rejected(self):
        caller = _FakeCaller(
            '{"propose": true, "proposed_text": "x", "confidence": 0.9, '
            '"evidence_ids": [99], "rationale": "y"}'
        )
        episodes = [
            {
                "episode_id": 1,
                "reaction_valence": -1,
                "followup": 0,
                "barged_in": 1,
                "text": "z",
            }
        ]
        result = await ambient_analysis.classify_scope(
            caller, "channel", "c1", episodes, "current"
        )
        assert result["confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_empty_episodes_never_calls_model(self):
        caller = _FakeCaller('{"propose": true}')
        result = await ambient_analysis.classify_scope(
            caller, "channel", "c1", [], "current"
        )
        assert result["confidence"] == 0.0
        assert caller.calls == 0
