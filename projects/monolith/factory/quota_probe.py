"""Refresh absent Claude quota through a bounded, ordinary inference request.

Only response headers supply quota. The probe's model output is never telemetry.
The persistent claim survives leader changes; unresolved probe sessions prevent
another request from accumulating behind a lost response.
"""

from __future__ import annotations

import asyncio
from datetime import timezone
import logging
import math
from uuid import uuid4

from sqlalchemy import or_
from sqlmodel import select

from factory.execution.models import AgentSession, PendingMessage
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


def _cleanup_candidates() -> list[str]:
    """Only completed, known-outcome probe turns can release their guests."""
    with controls._locked_session() as (db, _control):
        pending = (
            select(PendingMessage.id)
            .where(PendingMessage.session_id == AgentSession.id)
            .exists()
        )
        rows = db.exec(
            select(AgentSession)
            .where(
                or_(
                    AgentSession.local_session_id.startswith(PREFIX),
                    AgentSession.local_session_id.startswith(REVIEW_PREFIX),
                ),
                AgentSession.ember_session_id.is_not(None),
                AgentSession.status != "running",
                ~pending,
            )
            .limit(5)
        ).all()
        return [
            row.ember_session_id
            for row in rows
            if store.guest_cleanup_hold(db, row.id, row.ember_session_id) is None
        ]


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


async def _loop() -> None:
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - dependency failures must not stop checks
            logger.exception("quota probe check failed")
        await asyncio.sleep(CHECK_SECONDS)


def start_quota_probe_loop() -> list[asyncio.Task]:
    from framework import log_task_exception

    task = asyncio.create_task(_loop(), name="factory-quota-probe")
    task.add_done_callback(log_task_exception)
    return [task]
