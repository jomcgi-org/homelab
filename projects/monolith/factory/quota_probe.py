"""Refresh absent Claude quota through a bounded, ordinary inference request.

Only response headers supply quota. The probe's model output is never telemetry.
The persistent claim survives leader changes; unresolved probe sessions prevent
another request from accumulating behind a lost response.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta, timezone
import logging
import math
from uuid import uuid4

from sqlalchemy import or_
from sqlmodel import select

from factory.execution.models import (
    AgentCapacityReservation,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from factory.execution.constants import UNKNOWN_INVOCATION
from factory.execution.review_leases import PREFIX as REVIEW_PREFIX
from factory.execution import store
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt
from factory.orchestration.factory_quota_guard import _window

logger = logging.getLogger(__name__)
ACTIVE_SECONDS = 15 * 60
QUIET_SECONDS = 60 * 60
TURN_TIMEOUT_SECONDS = 120
CHECK_SECONDS = 60
PREFIX = "synthetic:factory-quota:"
ACTOR = "factory:quota-probe"
PROMPT = (
    "Reply with exactly OK. Do not use tools, inspect files, or perform any other work."
)
CODEX_PREFIX = "synthetic:factory-codex-quota:"
CODEX_ACTOR = "factory:codex-quota-probe"
CODEX_STARTED_ACTION = "codex_quota_probe_started"
CODEX_OBSERVED_ACTION = "codex_quota_probe_observed"
CODEX_NO_OBSERVATION_ACTION = "codex_quota_probe_no_observation"
CODEX_FAILED_ACTION = "codex_quota_probe_failed"
# Four times the KG freshness ceiling (900s), thirty turn timeouts (120s).
# Only orphaned evidence expires here; bound or running guests always fence.
CODEX_ORPHAN_FENCE_SECONDS = 3600


def _codex_probe_model() -> str:
    """The model an unpinned Codex freshness probe runs on.

    Always the drainer model: the probe exists to refresh the observations
    the KG admission gate reads, so it must travel the same lane it measures.
    Imported late because the drainer pulls in the orchestration graph.
    """
    from factory.orchestration.drainer import DRAIN_MODEL

    return DRAIN_MODEL


def _fresh(reading: dict | None, interval: int) -> bool:
    if reading is None:
        return False
    age = reading.get("age_seconds")
    return (
        isinstance(age, (int, float))
        and not isinstance(age, bool)
        and math.isfinite(age)
        and 0 <= age < interval
    )


def claim(payload: dict) -> str | None:
    """Atomically reserve a probe, independent of reviewer fallback selection."""
    with controls._locked_session() as (db, control):
        # A broken broker cannot receive observations. Avoid spending a model
        # request merely to discover that the destination is still down.
        if not payload.get("available"):
            return None
        active = (
            db.exec(
                select(FactoryReceipt.id)
                .where(
                    FactoryReceipt.state.in_(["admitted", "uncertain"]),
                    FactoryReceipt.task_paused == False,  # noqa: E712
                )
                .limit(1)
            ).first()
            is not None
        )
        interval = ACTIVE_SECONDS if active else QUIET_SECONDS
        if _fresh(_window(payload), interval):
            return None
        previous = db.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action == "quota_probe_started")
            .order_by(FactoryAudit.id.desc())
        ).first()
        now = controls._now()
        if previous is not None:
            created = previous.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (now - created).total_seconds() < interval:
                return None
        # No second guest while a prior probe is running, still bound, or has
        # an unresolved pending turn. Session maintenance owns its recovery.
        pending = (
            select(PendingMessage.id)
            .where(PendingMessage.session_id == AgentSession.id)
            .exists()
        )
        unresolved = db.exec(
            select(AgentSession.id)
            .where(
                AgentSession.local_session_id.startswith(PREFIX),
                or_(
                    AgentSession.status == "running",
                    AgentSession.ember_session_id.is_not(None),
                    pending,
                    store._unknown_outcome_exists(AgentSession.id),
                ),
            )
            .limit(1)
        ).first()
        if unresolved is not None:
            return None
        key = PREFIX + str(uuid4())
        controls._audit(
            db,
            ACTOR,
            "quota_probe_started",
            session_key=key,
            model="haiku",
            interval_seconds=interval,
        )
        return key


def _record(key: str, action: str, **details) -> None:
    with controls._locked_session() as (db, _control):
        controls._audit(db, ACTOR, action, session_key=key, **details)


def _codex_grant_views(payload: dict) -> list[dict]:
    """Per-grant Codex views from one broker payload, summarized as observed."""
    from factory.execution.provider_quota import summarise_grants

    grants = payload.get("grants")
    views, _validity = summarise_grants(grants if isinstance(grants, dict) else {})
    return [view for view in views.values() if view.get("provider") == "codex"]


def _codex_grant_exhausted(view: dict) -> bool:
    """Whether one observed Codex grant can still be out of room.

    Mirrors the egress ranker's ``bandFor``: exhaustion holds only while it
    can still be true. An active window at the exhausted percent counts, and
    so does a latched exhausted flag while some active window resets in the
    future. A window-less rejection is exhausted only while its age is not
    provably stale, and a rejection whose resets have all passed is retryable,
    so an all-stale pool left by 429s is probed rather than wedged.
    """
    from factory.orchestration import model_pool as pool

    windows = [
        window
        for window in view.get("windows") or []
        if isinstance(window, dict)
        and pool.reset_passed(window.get("resets_at")) is not True
    ]
    threshold = pool.exhausted_percent()
    for window in windows:
        used = window.get("used_percent")
        if (
            isinstance(used, (int, float))
            and not isinstance(used, bool)
            and used >= threshold
        ):
            return True
    if view.get("exhausted") is not True:
        return False
    if not windows:
        return pool.grant_observation_state(view) != "stale"
    return any(pool.reset_passed(w.get("resets_at")) is False for w in windows)


def _codex_fresh(payload: dict) -> bool:
    """Whether any observed Codex grant carries a fresh observation."""
    from factory.orchestration import model_pool as pool

    return any(
        view.get("observed") is True and pool.grant_observation_state(view) == "fresh"
        for view in _codex_grant_views(payload)
    )


def codex_claim(payload: dict) -> str | None:
    """Reserve one unpinned Codex freshness probe, or None when it cannot help.

    The probe fires only into an all-stale Codex pool: the broker is
    available, at least one grant is observed, none is fresh, and at least
    one observed grant is not exhausted. A fresh grant present, an
    all-exhausted pool, a missing observation, a claim inside the
    rate-limit window, or an unresolved prior Codex probe all suppress it.
    The probe only refreshes evidence for the KG gate, it never admits work
    itself, and its audit rows and session prefix are disjoint from the
    Claude probe so neither lane blocks the other.
    """
    from factory.orchestration import model_pool as pool

    if not payload.get("available"):
        return None
    observed = [
        view for view in _codex_grant_views(payload) if view.get("observed") is True
    ]
    if not observed:
        return None
    if any(pool.grant_observation_state(view) == "fresh" for view in observed):
        return None
    if all(_codex_grant_exhausted(view) for view in observed):
        return None
    model = _codex_probe_model()
    with controls._locked_session() as (db, control):
        interval = pool.kg_quota_max_age_seconds()
        previous = db.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action == CODEX_STARTED_ACTION)
            .order_by(FactoryAudit.id.desc())
        ).first()
        now = controls._now()
        if previous is not None:
            created = previous.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (now - created).total_seconds() < interval:
                return None
        # Orphan evidence can outlive its guest. Age each pending/unknown row,
        # never the session: recent evidence on an old session still fences.
        cutoff = now - timedelta(seconds=CODEX_ORPHAN_FENCE_SECONDS)
        pending = (
            select(PendingMessage.id)
            .where(
                PendingMessage.session_id == AgentSession.id,
                PendingMessage.created_at >= cutoff,
            )
            .exists()
        )
        unknown = or_(
            select(AgentTurn.id)
            .where(
                AgentTurn.session_id == AgentSession.id,
                AgentTurn.stop_reason == UNKNOWN_INVOCATION,
                AgentTurn.created_at >= cutoff,
            )
            .exists(),
            select(AgentCapacityReservation.id)
            .where(
                AgentCapacityReservation.session_id == AgentSession.id,
                AgentCapacityReservation.state == "uncertain",
                AgentCapacityReservation.created_at >= cutoff,
            )
            .exists(),
        )
        unresolved = db.exec(
            select(AgentSession, pending, unknown)
            .where(
                AgentSession.local_session_id.startswith(CODEX_PREFIX),
                or_(
                    AgentSession.status == "running",
                    AgentSession.ember_session_id.is_not(None),
                    pending,
                    unknown,
                ),
            )
            .order_by(AgentSession.id)
            .limit(1)
        ).first()
        if unresolved is not None:
            row, has_pending, _has_unknown = unresolved
            reason = (
                "bound guest"
                if row.ember_session_id is not None
                else "running"
                if row.status == "running"
                else "pending turn"
                if has_pending
                else "unknown outcome"
            )
            logger.info(
                "Codex quota probe suppressed: session %s has %s", row.id, reason
            )
            return None
        key = CODEX_PREFIX + str(uuid4())
        controls._audit(
            db,
            CODEX_ACTOR,
            CODEX_STARTED_ACTION,
            session_key=key,
            model=model,
            interval_seconds=interval,
        )
        return key


def _codex_record(key: str, action: str, **details) -> None:
    with controls._locked_session() as (db, _control):
        controls._audit(db, CODEX_ACTOR, action, session_key=key, **details)


def _cleanup_candidates() -> list[str]:
    """Only completed, known-outcome probe turns can release their guests."""
    with controls._locked_session() as (db, _control):
        pending = (
            select(PendingMessage.id)
            .where(PendingMessage.session_id == AgentSession.id)
            .exists()
        )
        guests = []
        after_id = 0
        # Advance past held rows in bounded query pages, as guest_cleanup does.
        # The output bound applies to unheld guests, not scanned candidates.
        while True:
            rows = db.exec(
                select(AgentSession)
                .where(
                    AgentSession.id > after_id,
                    or_(
                        AgentSession.local_session_id.startswith(PREFIX),
                        AgentSession.local_session_id.startswith(CODEX_PREFIX),
                        AgentSession.local_session_id.startswith(REVIEW_PREFIX),
                    ),
                    AgentSession.ember_session_id.is_not(None),
                    AgentSession.status != "running",
                    ~pending,
                )
                .order_by(AgentSession.id)
                .limit(5)
            ).all()
            for row in rows:
                if store.guest_cleanup_hold(db, row.id, row.ember_session_id) is None:
                    guests.append(row.ember_session_id)
                    if len(guests) == 5:
                        return guests
            if len(rows) < 5:
                return guests
            after_id = rows[-1].id


async def _cleanup() -> None:
    from factory.execution.execution_api import destroy_and_confirm
    from factory.execution.mcp import _clear_ember_bindings_for
    from factory.execution.transport import EmberSessionGone

    for guest in await asyncio.to_thread(_cleanup_candidates):
        try:
            confirmed = await destroy_and_confirm(guest)
        except EmberSessionGone:
            confirmed = True
        except Exception:
            logger.warning("quota probe cleanup remains unconfirmed")
            continue
        if confirmed:
            await asyncio.to_thread(_clear_ember_bindings_for, guest)


async def tick() -> None:
    from factory.execution.execution_api import run_synthetic_session
    from factory.execution.provider_quota import fetch_provider_quota

    await _cleanup()
    payload = await fetch_provider_quota(force=True)
    key = await asyncio.to_thread(claim, payload)
    if key is None:
        return
    try:
        async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
            await run_synthetic_session(
                PROMPT,
                model="haiku",
                session_key=key,
                read_timeout=TURN_TIMEOUT_SECONDS,
            )
        # Reporting is asynchronous in the egress sidecar. This is a bounded
        # observation wait, never a second inference attempt.
        for _ in range(5):
            payload = await fetch_provider_quota(force=True)
            if _fresh(_window(payload), CHECK_SECONDS):
                await asyncio.to_thread(_record, key, "quota_probe_observed")
                return
            await asyncio.sleep(1)
        await asyncio.to_thread(_record, key, "quota_probe_no_observation")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the next scheduled check remains alive
        logger.warning("quota probe failed: %s", type(exc).__name__)
        await asyncio.to_thread(
            _record, key, "quota_probe_failed", error=type(exc).__name__
        )


async def codex_tick() -> None:
    """Send one unpinned Luna probe when every observed Codex grant is stale.

    The request travels the normal synthetic-session path with no per-grant
    pinning, so the egress ranker routes it to the best usable grant and the
    broker records that grant's observation, which reopens the KG gate. A
    stale pool with no fresh grant to prefer is exactly when the ranker
    serves the probe on a stale grant and refreshes it either way.
    """
    from factory.execution.execution_api import run_synthetic_session
    from factory.execution.provider_quota import fetch_provider_quota

    payload = await fetch_provider_quota(force=True)
    key = await asyncio.to_thread(codex_claim, payload)
    if key is None:
        return
    try:
        async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
            await run_synthetic_session(
                PROMPT,
                model=_codex_probe_model(),
                session_key=key,
                read_timeout=TURN_TIMEOUT_SECONDS,
            )
        # Reporting is asynchronous in the egress sidecar. This is a bounded
        # observation wait, never a second inference attempt.
        for _ in range(5):
            payload = await fetch_provider_quota(force=True)
            if _codex_fresh(payload):
                await asyncio.to_thread(_codex_record, key, CODEX_OBSERVED_ACTION)
                return
            await asyncio.sleep(1)
        await asyncio.to_thread(_codex_record, key, CODEX_NO_OBSERVATION_ACTION)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the next scheduled check remains alive
        logger.warning("codex quota probe failed: %s", type(exc).__name__)
        await asyncio.to_thread(
            _codex_record, key, CODEX_FAILED_ACTION, error=type(exc).__name__
        )


async def _loop() -> None:
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - dependency failures must not stop checks
            logger.exception("quota probe check failed")
        try:
            await codex_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - dependency failures must not stop checks
            logger.exception("codex quota probe check failed")
        await asyncio.sleep(CHECK_SECONDS)


def start_quota_probe_loop() -> list[asyncio.Task]:
    from framework import log_task_exception

    task = asyncio.create_task(_loop(), name="factory-quota-probe")
    task.add_done_callback(log_task_exception)
    return [task]
