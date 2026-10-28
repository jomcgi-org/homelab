"""Real row-lock admission, probe and post-recognition authorization races."""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, select

from grimoire.models import (
    AppUser,
    Campaign,
    CampaignMember,
    GameSession,
    SessionEvent,
    SpeechControl,
    TranscriptConsent,
)
from grimoire.stt import Segment
from grimoire.stt.control import FAILURE_THRESHOLD, admit, finish
from grimoire.stt.routes import AudioContext, append_segment


@pytest.fixture
def speech(pg, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_TRANSCRIPT_ENABLED", "true")
    engine = create_engine(
        pg.url,
        poolclass=NullPool,
        connect_args={"options": "-c lock_timeout=5000 -c statement_timeout=10000"},
    )
    with Session(engine) as session:
        campaign = Campaign(name="Speech lock test")
        user = AppUser(email=f"speech-{uuid4()}@example.test")
        session.add_all([campaign, user])
        session.flush()
        game = GameSession(
            campaign_id=campaign.id, status="active", transcript_state="on"
        )
        member = CampaignMember(
            campaign_id=campaign.id, app_user_id=user.id, role="player"
        )
        session.add_all([game, member])
        session.flush()
        consent = TranscriptConsent(
            campaign_id=campaign.id,
            member_id=member.id,
            processor="Google Speech-to-Text",
        )
        session.add(consent)
        session.commit()
        context = AudioContext(engine, user.email, user.id, member.id)
        campaign_id, session_id, consent_id = campaign.id, game.id, consent.id
    try:
        yield engine, context, campaign_id, session_id, consent_id
    finally:
        with Session(engine) as session:
            session.execute(delete(Campaign).where(Campaign.id == campaign_id))
            session.execute(delete(AppUser).where(AppUser.id == context.user_id))
            session.execute(
                delete(SpeechControl).where(
                    SpeechControl.key.in_(
                        ["provider:google", f"member:{context.member_id}"]
                    )
                )
            )
            session.commit()
        engine.dispose()


def test_duplicate_admission_is_serialized_across_connections(speech):
    engine, context, _campaign, game, _consent = speech
    request_id = uuid4()

    def reserve():
        with Session(engine) as session:
            attempt, fresh = admit(session, game, context.member_id, request_id)
            identity = attempt.id
            session.commit()
            return identity, fresh

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: reserve(), range(4)))
    assert sum(fresh for _, fresh in results) == 1
    assert len({identity for identity, _ in results}) == 1


def test_only_one_recovery_probe_and_only_that_probe_clears_breaker(speech):
    engine, context, _campaign, game, _consent = speech
    with Session(engine) as session:
        old, _ = admit(session, game, context.member_id, uuid4())
        old_id = old.id
        session.commit()
        control = session.get(SpeechControl, "provider:google")
        control.failures = FAILURE_THRESHOLD
        control.open_until = 0
        session.commit()

    def probe():
        with Session(engine) as session:
            try:
                attempt, _fresh = admit(session, game, context.member_id, uuid4())
                identity = attempt.id
                session.commit()
                return identity
            except HTTPException as error:
                assert error.status_code == 503
                return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(lambda _: probe(), range(4)))
    probes = [value for value in identities if value]
    assert len(probes) == 1
    with Session(engine) as session:
        finish(session, old_id, success=True)
        session.commit()
        assert (
            session.get(SpeechControl, "provider:google").failures == FAILURE_THRESHOLD
        )
        finish(session, probes[0], success=True)
        session.commit()
        assert session.get(SpeechControl, "provider:google").failures == 0


@pytest.mark.parametrize("mutation", ["revoke", "pause", "end", "membership"])
def test_fresh_authorization_waits_for_inflight_state_write(speech, mutation):
    engine, context, campaign, game, consent_id = speech
    request_id = uuid4()
    with Session(engine) as session:
        attempt, _ = admit(session, game, context.member_id, request_id)
        attempt_id = attempt.id
        session.commit()
    started = Event()

    def append():
        started.set()
        return append_segment(
            context,
            campaign,
            game,
            request_id,
            attempt_id,
            Segment("Must not append", 0.9, 1),
            1,
        )

    with Session(engine) as writer, ThreadPoolExecutor(max_workers=1) as pool:
        if mutation == "revoke":
            writer.get(TranscriptConsent, consent_id).revoked_at = datetime.now(UTC)
        elif mutation == "membership":
            writer.delete(writer.get(CampaignMember, context.member_id))
        elif mutation == "pause":
            writer.get(GameSession, game).transcript_state = "paused"
        else:
            writer.get(GameSession, game).status = "ended"
        writer.flush()
        pending = pool.submit(append)
        assert started.wait(2)
        with pytest.raises(FutureTimeout):
            pending.result(timeout=0.2)
        writer.commit()
        with pytest.raises(HTTPException) as error:
            pending.result(timeout=5)
        assert error.value.status_code in {403, 404, 409}
    with Session(engine) as session:
        assert not session.exec(
            select(SessionEvent).where(
                SessionEvent.session_id == game, SessionEvent.kind == "utterance"
            )
        ).all()
