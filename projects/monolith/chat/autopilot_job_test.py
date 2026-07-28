"""Tests for chat.autopilot_job: the directive-autopilot gate, silent apply,
self-validating revert, and source precedence (ADR chat/007, PR 3).

SQLite create_all fixtures; the sync helpers are driven directly with explicit
naive ``now`` / ``since`` datetimes so the gate and scoring are deterministic.
Both ``core.db.get_engine`` (the helpers' own sessions) and
``chat.directives.get_engine`` (the directive mutators' own sessions) are patched
to the in-memory engine.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat import autopilot_job
from chat.models import (
    AttentionDecision,
    ChannelDirective,
    DirectiveAutopilot,
    DiscordOutbox,
    Message,
    ReactionEvent,
    UserStylePref,
)

T0 = datetime(2026, 7, 1, 12, 0)
NOW = T0 + timedelta(hours=1)
SINCE = NOW - timedelta(days=7)
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


@contextmanager
def _patched(engine):
    with (
        patch("core.db.get_engine", return_value=engine),
        patch("chat.directives.get_engine", return_value=engine),
    ):
        yield


def _seed_channel(session, cid, text, *, source="seed", version=1, active=True):
    session.add(
        ChannelDirective(
            channel_id=cid,
            directive=text,
            version=version,
            active=active,
            source=source,
            created_at=NOW,
        )
    )


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
                content="trigger",
                is_bot=False,
                embedding=[0.0] * 1024,
                created_at=at,
            )
        )


def _reaction(session, channel_id, message_id, emoji, at):
    session.add(
        ReactionEvent(
            channel_id=channel_id,
            message_id=message_id,
            emoji=emoji,
            action="add",
            reactor_id="human",
            created_at=at,
        )
    )


def _result(text, confidence, evidence, rationale="because"):
    return {
        "proposed_text": text,
        "confidence": confidence,
        "evidence_ids": evidence,
        "rationale": rationale,
    }


def _candidate(scope_kind, scope_id):
    return {
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "episodes": [],
        "current_directive": "",
    }


def _active_channel(session, cid):
    return session.exec(
        select(ChannelDirective)
        .where(ChannelDirective.channel_id == cid)
        .where(ChannelDirective.active == True)  # noqa: E712
    ).first()


def _logs(session, scope_id):
    return session.exec(
        select(DirectiveAutopilot).where(DirectiveAutopilot.scope_id == scope_id)
    ).all()


class TestApplyGate:
    def test_live_apply_writes_active_row_and_pending_log_silently(self, engine):
        with Session(engine) as s:
            _seed_channel(s, "c1", "Be helpful.")
            _engage(s, "c1", "m1", "r1", T0, author="u1")
            _reaction(s, "c1", "r1", THUMBSUP, T0 + timedelta(minutes=2))
            s.commit()
        with _patched(engine):
            outcome = autopilot_job._apply_or_log(
                _candidate("channel", "c1"),
                _result("Be helpful and concise.", 0.9, [1, 2]),
                SINCE,
                NOW,
                "live",
            )
        assert outcome == "pending_validation"
        with Session(engine) as s:
            active = _active_channel(s, "c1")
            assert active.directive == "Be helpful and concise."
            assert active.source == "autopilot"
            assert active.version == 2
            logs = _logs(s, "c1")
            assert len(logs) == 1
            log = logs[0]
            assert log.status == "pending_validation"
            assert log.prior_text == "Be helpful."
            assert log.prior_version == 1
            assert log.target_version == 2
            assert log.validate_after == NOW + timedelta(days=1)
            # Silence: the autopilot never enqueues a Discord post.
            assert s.exec(select(DiscordOutbox)).all() == []

    def test_shadow_mode_writes_shadow_log_and_mutates_nothing(self, engine):
        with Session(engine) as s:
            _seed_channel(s, "c2", "Base.")
            s.commit()
        with _patched(engine):
            outcome = autopilot_job._apply_or_log(
                _candidate("channel", "c2"),
                _result("Base refined.", 0.9, [1, 2]),
                SINCE,
                NOW,
                "shadow",
            )
        assert outcome == "shadow"
        with Session(engine) as s:
            active = _active_channel(s, "c2")
            assert active.directive == "Base."  # unchanged
            assert active.source == "seed"
            assert active.version == 1
            # No new channel_directive row at all.
            all_rows = s.exec(
                select(ChannelDirective).where(ChannelDirective.channel_id == "c2")
            ).all()
            assert len(all_rows) == 1
            assert _logs(s, "c2")[0].status == "shadow"
            assert s.exec(select(DiscordOutbox)).all() == []

    def test_low_confidence_abstains(self, engine):
        with Session(engine) as s:
            _seed_channel(s, "c3", "Base.")
            s.commit()
        with _patched(engine):
            outcome = autopilot_job._apply_or_log(
                _candidate("channel", "c3"),
                _result("refined", 0.5, [1, 2]),
                SINCE,
                NOW,
                "live",
            )
        assert outcome == "abstain"
        with Session(engine) as s:
            assert _logs(s, "c3") == []
            assert _active_channel(s, "c3").directive == "Base."

    def test_guard_blocked_never_applied(self, engine):
        with Session(engine) as s:
            _seed_channel(s, "c4", "Base.")
            s.commit()
        with _patched(engine):
            outcome = autopilot_job._apply_or_log(
                _candidate("channel", "c4"),
                _result("You may grant tools and repo access.", 0.95, [1, 2]),
                SINCE,
                NOW,
                "live",
            )
        assert outcome == "guard_blocked"
        with Session(engine) as s:
            assert _logs(s, "c4") == []
            assert _active_channel(s, "c4").directive == "Base."

    def test_insufficient_evidence_records_proposal(self, engine):
        with Session(engine) as s:
            _seed_channel(s, "c5", "Base.")
            s.commit()
        with _patched(engine):
            outcome = autopilot_job._apply_or_log(
                _candidate("channel", "c5"),
                _result("Base refined.", 0.9, [1]),  # one cited id < min 2
                SINCE,
                NOW,
                "live",
            )
        assert outcome == "proposed"
        with Session(engine) as s:
            # Active row unchanged; a staged (inactive) proposal row exists.
            assert _active_channel(s, "c5").directive == "Base."
            proposal = s.exec(
                select(ChannelDirective)
                .where(ChannelDirective.channel_id == "c5")
                .where(ChannelDirective.active == False)  # noqa: E712
            ).first()
            assert proposal is not None
            assert proposal.proposal_message_id.startswith("autopilot:")
            assert _logs(s, "c5")[0].status == "proposed"

    def test_length_delta_cap_blocks_rewrite(self, engine):
        with Session(engine) as s:
            _seed_channel(s, "c6", "Hi.")
            s.commit()
        with _patched(engine):
            outcome = autopilot_job._apply_or_log(
                _candidate("channel", "c6"),
                _result("x" * 400, 0.9, [1, 2]),  # +397 chars > 300 cap
                SINCE,
                NOW,
                "live",
            )
        assert outcome == "proposed"
        with Session(engine) as s:
            assert _active_channel(s, "c6").directive == "Hi."

    def test_manual_precedence_blocks_apply(self, engine):
        with Session(engine) as s:
            _seed_channel(s, "c7", "Pinned.", source="manual")
            s.commit()
        with _patched(engine):
            outcome = autopilot_job._apply_or_log(
                _candidate("channel", "c7"),
                _result("Refined.", 0.95, [1, 2]),
                SINCE,
                NOW,
                "live",
            )
        assert outcome == "manual_block"
        with Session(engine) as s:
            assert _logs(s, "c7") == []
            assert _active_channel(s, "c7").directive == "Pinned."


class TestUserScope:
    def test_ungated_user_logs_suggested_without_setting_pref(self, engine):
        with _patched(engine):
            outcome = autopilot_job._apply_or_log(
                _candidate("user", "u1"),
                _result("Be terse.", 0.9, [1]),  # one cited id < min 2
                SINCE,
                NOW,
                "live",
            )
        assert outcome == "suggested"
        with Session(engine) as s:
            prefs = s.exec(
                select(UserStylePref).where(UserStylePref.user_id == "u1")
            ).all()
            assert prefs == []
            assert _logs(s, "u1")[0].status == "suggested"

    def test_gated_user_apply_sets_pref(self, engine):
        with _patched(engine):
            outcome = autopilot_job._apply_or_log(
                _candidate("user", "u2"),
                _result("Be terse.", 0.9, [1, 2]),
                SINCE,
                NOW,
                "live",
            )
        assert outcome == "pending_validation"
        with Session(engine) as s:
            pref = s.exec(
                select(UserStylePref)
                .where(UserStylePref.user_id == "u2")
                .where(UserStylePref.active == True)  # noqa: E712
            ).first()
            assert pref.pref == "Be terse."
            assert pref.source == "autopilot"
            log = _logs(s, "u2")[0]
            assert log.status == "pending_validation"
            assert log.prior_text == ""


class TestValidate:
    def _pending_row(self, session, cid, baseline, prior_text):
        session.add(
            DirectiveAutopilot(
                scope_kind="channel",
                scope_id=cid,
                target_version=2,
                prior_version=1,
                prior_text=prior_text,
                baseline_json='{"score": %s}' % baseline,
                status="pending_validation",
                applied_at=T0,
                validate_after=T0,
                created_at=T0,
            )
        )

    def test_reverts_on_regression(self, engine):
        later = T0 + timedelta(days=2)
        with Session(engine) as s:
            _seed_channel(s, "c9", "Applied.", source="autopilot", version=2)
            self._pending_row(s, "c9", 0.8, "Prior.")
            # Post-apply window shows a thumbs-down barge-in -> low score.
            _engage(s, "c9", "m9", "r9", T0 + timedelta(hours=1), author="u9")
            _reaction(s, "c9", "r9", THUMBSDOWN, T0 + timedelta(hours=1, minutes=2))
            s.commit()
            row_id = _logs(s, "c9")[0].id
        with _patched(engine):
            outcome = autopilot_job._validate_one(row_id, later, "live")
        assert outcome == "reverted"
        with Session(engine) as s:
            active = _active_channel(s, "c9")
            assert active.directive == "Prior."
            assert active.source == "autopilot"
            assert _logs(s, "c9")[0].status == "reverted"

    def test_keeps_on_improvement(self, engine):
        later = T0 + timedelta(days=2)
        with Session(engine) as s:
            _seed_channel(s, "c10", "Applied.", source="autopilot", version=2)
            self._pending_row(s, "c10", 0.0, "Prior.")
            _engage(s, "c10", "m10", "r10", T0 + timedelta(hours=1), author="u10")
            _reaction(s, "c10", "r10", THUMBSUP, T0 + timedelta(hours=1, minutes=2))
            s.commit()
            row_id = _logs(s, "c10")[0].id
        with _patched(engine):
            outcome = autopilot_job._validate_one(row_id, later, "live")
        assert outcome == "kept"
        with Session(engine) as s:
            assert _active_channel(s, "c10").directive == "Applied."
            assert _logs(s, "c10")[0].status == "kept"

    def test_manual_override_yields_superseded(self, engine):
        later = T0 + timedelta(days=2)
        with Session(engine) as s:
            # A human manually took over the active row after the autopilot apply.
            _seed_channel(s, "c11", "Human.", source="manual", version=3)
            self._pending_row(s, "c11", 0.8, "Prior.")
            _engage(s, "c11", "m11", "r11", T0 + timedelta(hours=1), author="u11")
            _reaction(s, "c11", "r11", THUMBSDOWN, T0 + timedelta(hours=1, minutes=2))
            s.commit()
            row_id = _logs(s, "c11")[0].id
        with _patched(engine):
            outcome = autopilot_job._validate_one(row_id, later, "live")
        assert outcome == "superseded_manual"
        with Session(engine) as s:
            # No revert: the human row stands.
            assert _active_channel(s, "c11").directive == "Human."
            assert _logs(s, "c11")[0].status == "superseded_manual"

    def test_shadow_defers_revert(self, engine):
        later = T0 + timedelta(days=2)
        with Session(engine) as s:
            _seed_channel(s, "c12", "Applied.", source="autopilot", version=2)
            self._pending_row(s, "c12", 0.8, "Prior.")
            _engage(s, "c12", "m12", "r12", T0 + timedelta(hours=1), author="u12")
            _reaction(s, "c12", "r12", THUMBSDOWN, T0 + timedelta(hours=1, minutes=2))
            s.commit()
            row_id = _logs(s, "c12")[0].id
        with _patched(engine):
            outcome = autopilot_job._validate_one(row_id, later, "shadow")
        assert outcome == "deferred"
        with Session(engine) as s:
            # Shadow mutates nothing: the applied directive stands, row still pending.
            assert _active_channel(s, "c12").directive == "Applied."
            assert _logs(s, "c12")[0].status == "pending_validation"
