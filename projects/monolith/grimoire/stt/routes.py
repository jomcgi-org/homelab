"""Memory-only audio boundary. Synchronous DB phases each own their Session."""

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from core.db import get_session
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from opentelemetry import trace
from sqlmodel import Session, select

from grimoire.access import get_authenticated_email
from grimoire.models import (
    CampaignMember,
    GameSession,
    PlayerCharacter,
    SessionEvent,
    TranscriptConsent,
)
from grimoire.session_events import require_transcript_enabled
from grimoire.stt import ProviderUnavailable
from grimoire.stt.control import admit, finish, unavailable
from grimoire.stt.media import read_audio, validate_media
from grimoire.visibility import visible_entities_query

router = APIRouter()
tracer = trace.get_tracer(__name__)


@dataclass(frozen=True)
class AudioContext:
    engine: object
    email: str
    user_id: str | None
    member_id: str


def check_capture(session, campaign_id, session_id, email):
    from grimoire.router import _get_member_or_404

    require_transcript_enabled()
    member = _get_member_or_404(session, campaign_id, email)
    game = session.exec(
        select(GameSession)
        .where(GameSession.id == session_id, GameSession.campaign_id == campaign_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if game is None:
        raise HTTPException(404, "game session not found")
    if game.status == "ended" or game.transcript_state != "on":
        raise HTTPException(409, "transcript is not on")
    # Lock the current membership only after the session, before consent.
    member = session.exec(
        select(CampaignMember)
        .where(CampaignMember.id == member.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if member is None:
        raise HTTPException(404, "campaign not found")
    consent = session.exec(
        select(TranscriptConsent)
        .where(
            TranscriptConsent.campaign_id == campaign_id,
            TranscriptConsent.member_id == member.id,
            TranscriptConsent.revoked_at.is_(None),
        )
        .with_for_update(read=True)
    ).first()
    if consent is None:
        raise HTTPException(403, "active transcript consent required")
    # Existing text capture accepts other processors; audio must match this
    # route's actual processor, including after a mid-request consent change.
    if consent.processor != "Google Speech-to-Text":
        raise HTTPException(403, "Google Speech-to-Text consent required")
    return member


def speaker_hints(session, campaign_id, member):
    from grimoire.router import _viewer_for_member

    viewer = _viewer_for_member(session, campaign_id, member)
    rows = session.exec(visible_entities_query(campaign_id, viewer).limit(500)).all()
    names = [entity.name for entity, _ in rows]
    characters = select(PlayerCharacter).where(
        PlayerCharacter.campaign_id == campaign_id
    )
    if viewer != "dm":
        characters = characters.where(PlayerCharacter.id == member.player_character_id)
    names.extend(
        row.character_name for row in session.exec(characters.limit(100)).all()
    )
    return tuple(
        sorted(
            {name.strip() for name in names if name and 0 < len(name.strip()) <= 100}
        )
    )[:100]


def authorized_context(
    campaign_id: str,
    session_id: str,
    email: Annotated[str, Depends(get_authenticated_email)],
    session: Annotated[Session, Depends(get_session)],
):
    try:
        member = check_capture(session, campaign_id, session_id, email)
        return AudioContext(
            session.get_bind(),
            email,
            session.info.get("grimoire_user_id"),
            member.id,
        )
    finally:
        session.rollback()


def _new_session(context):
    session = Session(context.engine)
    if context.user_id:
        session.info["grimoire_user_id"] = context.user_id
    return session


def reserve(context, campaign_id, session_id, request_id):
    from grimoire.router import _event_view

    with _new_session(context) as session:
        member = check_capture(session, campaign_id, session_id, context.email)
        attempt, fresh = admit(session, session_id, member.id, request_id)
        if not fresh:
            if attempt.state == "complete":
                event = (
                    session.get(SessionEvent, attempt.event_id)
                    if attempt.event_id
                    else None
                )
                return (
                    attempt.id,
                    _event_view(event, member) if event else Response(status_code=204),
                    (),
                )
            # Includes crash, timeout and cancellation. Replaying never invokes
            # the provider again; callers use a new key for a new segment.
            raise HTTPException(409, "audio request pending or outcome unknown")
        identity = attempt.id
        hints = speaker_hints(session, campaign_id, member)
        session.commit()
        return identity, None, hints


def failed(context, attempt_id):
    with _new_session(context) as session:
        finish(session, attempt_id, success=False)
        session.commit()


def append_segment(
    context, campaign_id, session_id, request_id, attempt_id, segment, seconds
):
    from grimoire.models import SpeechAttempt
    from grimoire.router import UtteranceRequest, ingest_utterance_core

    with _new_session(context) as session:
        # New membership, session state and consent, after all provider I/O.
        check_capture(session, campaign_id, session_id, context.email)
        result = Response(status_code=204)
        if segment.text.strip():
            end = datetime.now(UTC)
            result = ingest_utterance_core(
                session,
                campaign_id,
                session_id,
                UtteranceRequest(
                    text=segment.text,
                    confidence=segment.confidence,
                    source="browser",
                    started_at=end - timedelta(seconds=seconds),
                    ended_at=end,
                    request_id=request_id,
                ),
                context.email,
            )
        finish(session, attempt_id, success=True)
        attempt = session.get(SpeechAttempt, attempt_id)
        attempt.state = "complete"
        attempt.event_id = result.id if not isinstance(result, Response) else None
        session.commit()
        return result


def transcriber():
    if os.getenv("GRIMOIRE_STT_PROVIDER") != "google":
        raise ProviderUnavailable() from None
    from grimoire.stt.google import GoogleTranscriber

    return GoogleTranscriber()


@router.post(
    "/campaigns/{campaign_id}/sessions/{session_id}/audio-segments",
    dependencies=[Depends(require_transcript_enabled)],
)
async def audio_segment(
    campaign_id: str,
    session_id: str,
    request: Request,
    context: Annotated[AudioContext, Depends(authorized_context)],
    request_id: Annotated[UUID, Header(alias="X-Request-ID")],
):
    audio = None
    attempt_id = None
    started = time.monotonic()
    # Exceptions contain no input, but disable automatic exception recording
    # as an additional boundary around future provider implementations.
    with tracer.start_as_current_span(
        "grimoire.stt.segment", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("grimoire.session_id", session_id)
        try:
            audio, mime = await read_audio(request)
            # Bounded CPU decode stays in this request. Audio is never placed
            # in an executor queue or handed to a background task.
            seconds = validate_media(audio, mime)
            attempt_id, replay, hints = await asyncio.to_thread(
                reserve, context, campaign_id, session_id, request_id
            )
            if replay is not None:
                return replay
            try:
                async with asyncio.timeout(10):
                    segment = await transcriber().transcribe(
                        audio, mime, "en-GB", hints
                    )
            except (ProviderUnavailable, TimeoutError):
                span.set_attribute("grimoire.stt.provider_error", "unavailable")
                span.set_attribute("grimoire.stt.billing", "unknown")
                await asyncio.to_thread(failed, context, attempt_id)
                raise unavailable() from None
            except asyncio.CancelledError:
                span.set_attribute("grimoire.stt.billing", "unknown")
                # Only receipt metadata enters this worker. Even if the client
                # disconnects, a pending key remains permanently non-retriable.
                await asyncio.shield(asyncio.to_thread(failed, context, attempt_id))
                raise
            finally:
                audio = None
            if segment.billed_seconds is not None:
                span.set_attribute(
                    "grimoire.stt.billed_seconds", segment.billed_seconds
                )
                span.set_attribute("grimoire.stt.billing", "confirmed")
            else:
                span.set_attribute("grimoire.stt.billing", "unknown")
            return await asyncio.to_thread(
                append_segment,
                context,
                campaign_id,
                session_id,
                request_id,
                attempt_id,
                segment,
                seconds,
            )
        finally:
            audio = None
            hints = ()
            span.set_attribute(
                "grimoire.stt.latency_ms", (time.monotonic() - started) * 1000
            )
