"""Pause delivery work while the shared Claude 7-day window is nearly spent.

Implementation capacity is elastic: brick nodes autoscale and the cheap
implementers bill someone else. Opus review is not. Every delivery task ends
in a review on the shared Claude subscription, so that window is the one input
the factory can actually exhaust, and running it to zero costs the operator
their own Claude sessions rather than only the factory's.

The guard reads the already-observed broker quota, never a provider directly.
It pauses delivery admissions and delivery node dispatch, leaves in-flight
nodes alone, and leaves the advisory lane alone entirely, because advisory
work passes no review gate. Its state lives in the audit ledger so a replica
restart cannot resume the lane by forgetting, and so the board can render it
without a broker call.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import time

from sqlmodel import Session, select

from swarm.factory_controls import (
    QUOTA_GUARD_MAX_AGE_SECONDS,
    _audit,
    _locked_session,
    _now,
    quota_guard_policy,
    quota_guard_state,
)
from swarm.factory_models import FactoryAudit

logger = logging.getLogger(__name__)

ACTOR = "factory:quota-guard"
WINDOW = "7d"
PROVIDER = "claude"
# One reading per tick at most. The broker client has its own thirty-second
# cache; this bounds the guard's own re-reads so a lane ticking every fifteen
# seconds does not ask four times a minute for a number that moves hourly.
READING_TTL_SECONDS = 60.0
# An unknown reading is the steady state of a broken broker, so its audit is
# throttled the way the idle intake audit is rather than written every tick.
UNKNOWN_AUDIT_SECONDS = 3600

_cache: tuple[float, dict | None] | None = None


def reset_cache() -> None:
    """Clear the process-local reading cache."""
    global _cache
    _cache = None


def _window(fetched: object) -> dict | None:
    """The claude 7d window from one broker payload, or None if it says nothing."""
    if not isinstance(fetched, dict):
        return None
    providers = fetched.get("providers")
    claude = providers.get(PROVIDER) if isinstance(providers, dict) else None
    if not isinstance(claude, dict) or not claude.get("observed", False):
        return None
    windows = claude.get("windows")
    if not isinstance(windows, list):
        return None
    age = claude.get("age_seconds")
    for window in windows:
        if not isinstance(window, dict) or window.get("name") != WINDOW:
            continue
        if window.get("expired", False):
            continue
        used = window.get("used_percent")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        # The egress sidecar reports Anthropic's utilisation header, which is a
        # fraction below one and a percentage above it. summarise() does not
        # normalise, so read it the same way the sidecar wrote it.
        value = float(used)
        return {
            "used_percent": value * 100.0 if value <= 1.0 else value,
            "age_seconds": (
                float(age)
                if isinstance(age, (int, float)) and not isinstance(age, bool)
                else None
            ),
            "resets_at": window.get("resets_at"),
        }
    return None


def reading(*, force: bool = False) -> dict | None:
    """The cached claude 7d observation, or None when there is nothing to read."""
    global _cache
    now = time.monotonic()
    if not force and _cache is not None and now - _cache[0] < READING_TTL_SECONDS:
        return _cache[1]
    try:
        from agent_sessions.provider_quota import fetch_provider_quota_sync

        observed = _window(fetch_provider_quota_sync())
    # nosemgrep: no-broad-except-swallow
    except Exception as exc:  # noqa: BLE001
        logger.warning("factory quota guard could not read provider quota: %s", exc)
        observed = None
    _cache = (now, observed)
    return observed


def _audit_unknown(db: Session, reason: str, detail: dict) -> None:
    """One unknown audit an hour, so a dead broker does not bury the ledger."""
    cutoff = _now() - timedelta(seconds=UNKNOWN_AUDIT_SECONDS)
    last = db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action == "quota_guard_unknown")
        .order_by(FactoryAudit.id.desc())
    ).first()
    if last is not None:
        created = last.created_at
        if created.tzinfo is None:
            from datetime import timezone

            created = created.replace(tzinfo=timezone.utc)
        if created >= cutoff:
            return
    _audit(db, ACTOR, "quota_guard_unknown", reason=reason, **detail)


def evaluate(policy: dict, *, session: Session | None = None) -> dict:
    """Decide whether delivery is paused, recording each transition once.

    An unknown or stale observation is not a pause. The guard exists to stop
    the factory spending a window it can see is nearly gone, not to stop it
    working whenever the broker is unreachable.
    """
    block = quota_guard_policy(policy)
    observed = reading()
    with _locked_session(session) as (db, _control):
        state = quota_guard_state(session=db)
        result = {
            "paused": False,
            "state": "unknown",
            "used_percent": None,
            "pause_percent": block["claude_7d_pause_percent"],
            "resume_percent": block["claude_7d_resume_percent"],
        }
        if observed is None:
            _audit_unknown(db, "unobserved", {})
            return result
        age = observed["age_seconds"]
        if isinstance(age, float) and age > QUOTA_GUARD_MAX_AGE_SECONDS:
            _audit_unknown(
                db,
                "stale",
                {"age_seconds": age, "used_percent": observed["used_percent"]},
            )
            return result
        used = observed["used_percent"]
        result["used_percent"] = used
        if state == "paused":
            if used < block["claude_7d_resume_percent"]:
                _audit(
                    db,
                    ACTOR,
                    "quota_guard_resumed",
                    used_percent=used,
                    resume_percent=block["claude_7d_resume_percent"],
                )
                result["state"] = "open"
                return result
            result.update(paused=True, state="paused")
            return result
        if used >= block["claude_7d_pause_percent"]:
            _audit(
                db,
                ACTOR,
                "quota_guard_paused",
                used_percent=used,
                pause_percent=block["claude_7d_pause_percent"],
                resets_at=observed.get("resets_at"),
            )
            result.update(paused=True, state="paused")
            return result
        result["state"] = "open"
        return result


def delivery_paused(*, session: Session | None = None) -> bool:
    """The durable verdict, for a caller that must not re-read the broker."""
    return quota_guard_state(session=session) == "paused"
