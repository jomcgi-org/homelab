"""Quota-aware model selection for factory roles.

A factory policy names one preferred model per role. An optional
``model_pools`` map extends each role to an ordered pool, and the conductor
picks the first pool member whose provider still has quota. The reviewer is
never selected here: review stays on its configured model so a walled provider
holds review rather than downgrading it.

Provider quota comes from the token broker's headline window. A provider with
no quota feed (Muse, pi) is always eligible. A provider whose quota is
unobserved is also eligible, because routing on a missing observation is worse
than routing on the operator's stated preference. Only positive evidence of
exhaustion moves selection down the pool.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ROLES = ("conductor", "worker")
_ROLE_KEY = {"conductor": "conductor_model", "worker": "worker_model"}
# Adapter family to broker provider. Families absent here have no quota feed.
QUOTA_PROVIDERS = {"codex": "codex", "claude": "claude"}
# Models at or above the ADR agents/038 judgment floor. Adapter family is NOT
# the test: sonnet shares the claude family with opus and sits below the floor.
JUDGMENT_MODELS = ("opus", "fable")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring malformed %s=%r; using %g", name, raw, default)
        return default


def exhausted_percent() -> float:
    return _env_float("SWARM_MODEL_POOL_EXHAUSTED_PERCENT", 97.0)


def max_quota_age_seconds() -> float:
    return _env_float("SWARM_MODEL_POOL_QUOTA_MAX_AGE_SECONDS", 900.0)


def quota_floors() -> dict:
    """Per-class, per-role floors on remaining quota, from SWARM_QUOTA_FLOORS.

    Shape: {"codex": {"worker": 10, "conductor": 0}, "claude": {"worker": 15}}.
    A role below its floor is walled for that class and spills through its
    pool; a role with a lower floor keeps drawing, so the last slice of a
    class goes to whichever role the operator ranked highest. Empty (the
    default) applies no floors.
    """
    raw = os.environ.get("SWARM_QUOTA_FLOORS", "")
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("ignoring malformed SWARM_QUOTA_FLOORS")
        return {}
    floors: dict = {}
    if isinstance(parsed, dict):
        for provider, roles in parsed.items():
            if not isinstance(roles, dict):
                continue
            floors[provider] = {
                role: float(value)
                for role, value in roles.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
    return floors


def floor_for(provider: str, role: str | None) -> float:
    if role is None:
        return 0.0
    return float(quota_floors().get(provider, {}).get(role, 0.0))


def pool_for(role: str, policy: dict) -> list[str]:
    """Return the ordered pool for a role, defaulting to its single model."""
    if role not in ROLES:
        raise ValueError(f"unknown factory role {role!r}")
    pools = policy.get("model_pools") or {}
    pool = pools.get(role) if isinstance(pools, dict) else None
    if isinstance(pool, list) and pool:
        return [str(model) for model in pool]
    return [policy[_ROLE_KEY[role]]]


def judgment_floor(policy: dict, *, quota: dict | None = None) -> dict:
    """The first pool member at or above the Opus floor, worker pool first.

    Falls back to the conductor pool when the worker pool names nothing at the
    floor, and to the conductor model itself when neither does, because a
    judgment task must never run on the cheap lane just because a policy was
    written without one. Provider quota is deliberately ignored: the floor is
    a capability constraint, and a quota-walled Opus holds work instead of
    demoting it. ``quota`` is accepted only to make that contract testable.
    """
    del quota
    worker_pool = pool_for("worker", policy)
    preferred = worker_pool[0]
    skipped: list[dict] = []

    def choice(model: str, reason: str) -> dict:
        return {
            "model": model,
            "preferred": preferred,
            "fallback_from": None if model == preferred else preferred,
            "skipped": skipped,
            "reason": reason,
        }

    for model in worker_pool:
        if model in JUDGMENT_MODELS:
            return choice(model, "judgment_floor_worker_pool")
        skipped.append({"model": model, "reason": "below_judgment_floor"})
    for model in pool_for("conductor", policy):
        if model in JUDGMENT_MODELS:
            return choice(model, "judgment_floor_conductor_pool")
        skipped.append({"model": model, "reason": "below_judgment_floor"})
    return choice(policy["conductor_model"], "judgment_floor_conductor_model")


def family_for(model: str) -> str | None:
    """Adapter family for a supported model, None for an unsupported name."""
    from agent_sessions import model_family

    try:
        return model_family(model)
    except ValueError:
        return None


def reset_passed(resets_at: object, now: datetime | None = None) -> bool | None:
    """True if a known reset time has passed, False if not yet, None if unknown."""
    if isinstance(resets_at, bool) or resets_at in (None, ""):
        return None
    if isinstance(resets_at, (int, float)):
        reset = datetime.fromtimestamp(float(resets_at), tz=timezone.utc)
    elif isinstance(resets_at, str):
        try:
            reset = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=timezone.utc)
    else:
        return None
    return (now or datetime.now(timezone.utc)) >= reset


def availability(model: str, quota: dict, role: str | None = None) -> tuple[bool, str]:
    """Decide whether a model's provider has quota for a role, with the reason.

    Exhaustion is trusted only while it can still be true: until a known
    reset time, or while the observation is fresh when no reset time is
    known. The broker records observations only on real provider traffic,
    so a lane that has routed away from a provider stops refreshing it; an
    exhausted flag that never expired would latch the fallback past the
    reset (#5803 covers the broker side of the same contract).
    """
    family = family_for(model)
    if family is None:
        return False, "unsupported_model"
    provider = QUOTA_PROVIDERS.get(family)
    if provider is None:
        return True, "no_quota_feed"
    summary = quota.get(provider)
    if not isinstance(summary, dict):
        return True, "unobserved"
    used = summary.get("headline_used_percent")
    age = summary.get("age_seconds")
    floor = floor_for(provider, role)
    has_used = isinstance(used, (int, float)) and not isinstance(used, bool)
    below_floor = has_used and floor > 0 and (100.0 - used) < floor
    walled = (
        bool(summary.get("exhausted"))
        or (has_used and used >= exhausted_percent())
        or below_floor
    )
    if not walled:
        return True, "available"
    if summary.get("exhausted"):
        reason = "exhausted"
    elif has_used and used >= exhausted_percent():
        reason = f"used_percent {used:g}"
    else:
        reason = f"below_floor {floor:g} remaining {100.0 - used:g}"
    passed = reset_passed(summary.get("resets_at"))
    if passed is True:
        return True, "reset_passed"
    if passed is False:
        return False, reason
    if isinstance(age, (int, float)) and age > max_quota_age_seconds():
        return True, f"stale_observation age {age:g}"
    return False, reason


def rollup_grants(summary: dict, grants: dict) -> dict:
    """Fold per-grant views into the class view a pool member is judged on.

    With more than one account on a class the provider-level view is only
    the latest report, whichever grant made it. The class has room while any
    grant does, so the class view takes the least-used non-exhausted grant
    (its used percent, reset time and age) and is exhausted only when every
    reporting grant is. Classes with no reporting grant are left untouched.
    """
    merged = dict(summary)
    by_provider: dict[str, list[dict]] = {}
    for view in grants.values():
        provider = view.get("provider")
        if isinstance(provider, str) and view.get("observed"):
            by_provider.setdefault(provider, []).append(view)
    for provider, views in by_provider.items():
        open_views = [v for v in views if not v.get("exhausted")]
        if not open_views:
            best = min(views, key=lambda v: v.get("age_seconds") or 0.0)
            merged[provider] = {**best, "exhausted": True}
            continue
        # A grant with no usable window says nothing about room: it sorts
        # last, so it can only win when it is the only open grant.
        best = min(
            open_views,
            key=lambda v: (
                v.get("headline_used_percent")
                if isinstance(v.get("headline_used_percent"), (int, float))
                and not isinstance(v.get("headline_used_percent"), bool)
                else float("inf")
            ),
        )
        merged[provider] = {**best, "exhausted": False}
    return merged


def quota_summary() -> dict:
    """Read the broker quota summary, treating any failure as unobserved."""
    try:
        from agent_sessions.provider_quota import (
            fetch_provider_quota_sync,
            summarise,
            summarise_grants,
        )

        fetched = fetch_provider_quota_sync()
        providers = fetched.get("providers", {}) if isinstance(fetched, dict) else {}
        summary = summarise(providers if isinstance(providers, dict) else {})
        grants = fetched.get("grants") if isinstance(fetched, dict) else None
        return rollup_grants(summary, summarise_grants(grants))
    # nosemgrep: no-broad-except-swallow
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider quota unavailable for model selection: %s", exc)
        return {}


def select_model(role: str, policy: dict, *, quota: dict | None = None) -> dict:
    """Pick the first pool member with quota, falling back to the preferred model."""
    pool = pool_for(role, policy)
    preferred = pool[0]
    if len(pool) == 1:
        # Nothing to choose between: never make the planner tick wait on the
        # broker for an answer that cannot change.
        return {
            "model": preferred,
            "preferred": preferred,
            "fallback_from": None,
            "skipped": [],
            "reason": "single_member_pool",
        }
    if quota is None:
        quota = quota_summary()
    skipped: list[dict] = []
    for model in pool:
        ok, reason = availability(model, quota, role)
        if ok:
            if model != preferred:
                logger.warning(
                    "factory %s model fallback %s -> %s (%s)",
                    role,
                    preferred,
                    model,
                    "; ".join(f"{s['model']} {s['reason']}" for s in skipped),
                )
            return {
                "model": model,
                "preferred": preferred,
                "fallback_from": None if model == preferred else preferred,
                "skipped": skipped,
                "reason": reason,
            }
        skipped.append({"model": model, "reason": reason})
    logger.warning(
        "factory %s pool has no provider with quota; keeping %s", role, preferred
    )
    return {
        "model": preferred,
        "preferred": preferred,
        "fallback_from": None,
        "skipped": skipped,
        "reason": "pool_exhausted",
    }


def selection_reason(reason: str, choice: dict, limit: int = 256) -> str:
    """Append the fallback evidence to a graph stated reason."""
    if choice.get("fallback_from") is None:
        return reason
    skipped = ", ".join(f"{s['model']} {s['reason']}" for s in choice["skipped"])
    text = f"{reason} (model fallback {choice['fallback_from']} -> {choice['model']}: {skipped})"
    return text[:limit]
