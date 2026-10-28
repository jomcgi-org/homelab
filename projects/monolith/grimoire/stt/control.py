"""PostgreSQL-serialized admission and provider recovery, shared by replicas."""

import os
import time
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlmodel import select

from grimoire.models import SpeechAttempt, SpeechControl

RATE_PER_MINUTE = 12
FAILURE_THRESHOLD = 3
RECOVERY_SECONDS = 30
PROBE_SECONDS = 15


def locked_control(session, key):
    # Absent rows cannot be locked. ON CONFLICT serializes their creation too.
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    session.execute(
        insert(SpeechControl)
        .values(
            key=key,
            window_started=0,
            count=0,
            failures=0,
            open_until=0,
            probe_until=0,
        )
        .on_conflict_do_nothing(index_elements=["key"])
    )
    return session.exec(
        select(SpeechControl)
        .where(SpeechControl.key == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def unavailable():
    return HTTPException(
        503,
        detail={"degraded": True, "message": "transcription unavailable"},
        headers={"Retry-After": str(RECOVERY_SECONDS)},
    )


def provider_status(session):
    if os.getenv("GRIMOIRE_STT_PROVIDER") != "google":
        return "unavailable"
    row = session.get(SpeechControl, "provider:google")
    return "degraded" if row and row.failures >= FAILURE_THRESHOLD else "available"


def admit(session, session_id, member_id, request_id, now=None):
    """Call only after auth, state, consent and media checks. Caller commits."""
    now = time.time() if now is None else now
    provider = locked_control(session, "provider:google")
    key = str(uuid5(NAMESPACE_URL, f"stt:{session_id}:{member_id}:{request_id}"))
    attempt = session.get(SpeechAttempt, key)
    if attempt is not None:
        return attempt, False
    if provider.failures >= FAILURE_THRESHOLD:
        if now < provider.open_until or now < provider.probe_until:
            raise unavailable()
        provider.probe_until = now + PROBE_SECONDS
        provider.probe_id = key
    budget = locked_control(session, f"member:{member_id}")
    if now >= budget.window_started + 60:
        budget.window_started, budget.count = now, 0
    if budget.count >= RATE_PER_MINUTE:
        raise HTTPException(
            429, "audio rate limit reached", headers={"Retry-After": "60"}
        )
    budget.count += 1
    attempt = SpeechAttempt(id=key, session_id=session_id, member_id=member_id)
    session.add(attempt)
    session.flush()
    return attempt, True


def finish(session, attempt_id, *, success):
    """Settle provider health; None leaves health unchanged for client rejection."""
    provider = locked_control(session, "provider:google")
    attempt = session.get(SpeechAttempt, attempt_id)
    if success is None:
        # A rejected recovery probe proves neither health nor an outage. Release
        # only its lease so another member can test recovery immediately.
        if provider.probe_id == attempt_id:
            provider.probe_until = 0
            provider.probe_id = None
        if attempt is not None:
            attempt.state = "unknown"
    elif success:
        # An ordinary request admitted before an outage cannot clear a newer
        # open breaker. Only the single leased recovery probe can clear it.
        if provider.failures < FAILURE_THRESHOLD or provider.probe_id == attempt_id:
            provider.failures = 0
            provider.open_until = provider.probe_until = 0
            provider.probe_id = None
    else:
        provider.failures += 1
        if provider.failures >= FAILURE_THRESHOLD:
            provider.open_until = time.time() + RECOVERY_SECONDS
            provider.probe_until = 0
            provider.probe_id = None
        # Membership/session deletion may cascade the receipt during I/O.
        # Preserve the sanitized provider failure even when it no longer exists.
        if attempt is not None:
            attempt.state = "unknown"
