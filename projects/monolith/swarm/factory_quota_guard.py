"""Route review away from Opus while the shared Claude 7-day window is spent.

Every delivery task ends in an independent Opus review on the shared Claude
subscription, and that window is the one input the factory can exhaust:
implementation autoscales and the cheap implementers bill someone else.
Running it to zero costs the operator their own sessions, not only the
factory's.

The answer is to review on a cheaper model, not to stop delivering. Nothing
here pauses admission and nothing holds an implement node: while the window is
nearly spent, review nodes take the next member of the reviewer pool that has
quota, and Opus comes back on its own once the window drops. Independence is a
property of the session a review runs in, never of the model it runs on, so a
fallback reviewer is as independent as Opus was.

Two things never fall back. Judgment-class work has a capability floor rather
than a price, so it waits for Opus. And when no member of the pool has quota,
review waits: review is the gate, and a gate that lets itself be skipped is
not one.

State lives in the audit ledger so a replica restart cannot forget a fallback
mid-task, and so the board can render it without a broker call.
"""

from __future__ import annotations

from datetime import timedelta, timezone
import json
import logging
import time

from sqlmodel import Session, select

from swarm.factory_controls import (
    JUDGMENT_CLASSES,
    QUOTA_GUARD_MAX_AGE_SECONDS,
    _audit,
    _locked_session,
    _now,
    latest_verdict,
    quota_guard_policy,
    review_routing_view,
    window_high,
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
            created = created.replace(tzinfo=timezone.utc)
        if created >= cutoff:
            return
    _audit(db, ACTOR, "quota_guard_unknown", reason=reason, **detail)


def _verdict_action(choice: dict) -> str:
    if choice["model"] is None:
        return "review_waiting"
    return (
        "reviewer_restored" if choice["fallback_from"] is None else "reviewer_fallback"
    )


def observe(policy: dict, *, session: Session | None = None) -> dict:
    """Re-read the window, decide how review routes, and record any change.

    Called once per tick before anything reconciles, because the review nodes
    those tasks are about to start are what spends the window.

    An unknown or stale reading never starts a fallback, because routing away
    from Opus on a broker outage would downgrade every review for no reason we
    can see. It does not end one either: a fallback entered at 95 percent would
    otherwise snap back to Opus the moment the broker went down, and spend the
    rest of the window with nothing able to say stop.
    """
    from swarm.model_pool import select_reviewer

    block = quota_guard_policy(policy)
    observed = reading()
    with _locked_session(session) as (db, _control):
        previous = latest_verdict(db)
        state = window_high(session=db)
        used = None
        if observed is None:
            _audit_unknown(db, "unobserved", {})
        else:
            age = observed["age_seconds"]
            if isinstance(age, float) and age > QUOTA_GUARD_MAX_AGE_SECONDS:
                _audit_unknown(
                    db,
                    "stale",
                    {"age_seconds": age, "used_percent": observed["used_percent"]},
                )
            else:
                used = observed["used_percent"]
                if state:
                    state = used >= block["claude_7d_resume_percent"]
                else:
                    state = used >= block["claude_7d_pause_percent"]
        choice = select_reviewer(policy, window_high=state)
        action = _verdict_action(choice)
        detail = {
            "window_high": state,
            "used_percent": used,
            "model": choice["model"],
            "pause_percent": block["claude_7d_pause_percent"],
            "resume_percent": block["claude_7d_resume_percent"],
            "skipped": choice["skipped"],
        }
        unchanged = (
            previous is not None
            and previous.action == action
            and _detail_model(previous) == choice["model"]
            and _detail_window(previous) == state
        )
        if not unchanged:
            _audit(db, ACTOR, action, **detail)
        return {"action": action, **detail}


def _detail(row: FactoryAudit) -> dict:
    try:
        return json.loads(row.detail_json)
    except (TypeError, ValueError):
        return {}


def _detail_model(row: FactoryAudit):
    return _detail(row).get("model")


def _detail_window(row: FactoryAudit) -> bool:
    return bool(_detail(row).get("window_high", False))


def reviewer_for(
    policy: dict, task_class: str, *, session: Session | None = None
) -> dict:
    """The model a review node must run on right now, or None to wait.

    Reads the recorded window state rather than the broker, so every node
    dispatched in one tick is routed by the same reading.
    """
    from swarm.model_pool import select_reviewer

    return select_reviewer(
        policy,
        window_high=window_high(session=session),
        judgment=task_class in JUDGMENT_CLASSES,
    )


# Re-exported so a caller that already has this module does not need to know
# the ledger read lives beside the policy it is read against.
routing_view = review_routing_view
