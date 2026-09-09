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

import logging
import os

logger = logging.getLogger(__name__)

ROLES = ("conductor", "worker")
_ROLE_KEY = {"conductor": "conductor_model", "worker": "worker_model"}
# Adapter family to broker provider. Families absent here have no quota feed.
QUOTA_PROVIDERS = {"codex": "codex", "claude": "claude"}


def exhausted_percent() -> float:
    return float(os.environ.get("SWARM_MODEL_POOL_EXHAUSTED_PERCENT", "97"))


def max_quota_age_seconds() -> float:
    return float(os.environ.get("SWARM_MODEL_POOL_QUOTA_MAX_AGE_SECONDS", "900"))


def pool_for(role: str, policy: dict) -> list[str]:
    """Return the ordered pool for a role, defaulting to its single model."""
    if role not in ROLES:
        raise ValueError(f"unknown factory role {role!r}")
    pools = policy.get("model_pools") or {}
    pool = pools.get(role) if isinstance(pools, dict) else None
    if isinstance(pool, list) and pool:
        return [str(model) for model in pool]
    return [policy[_ROLE_KEY[role]]]


def provider_for(model: str) -> str | None:
    from agent_sessions import model_family

    try:
        return QUOTA_PROVIDERS.get(model_family(model))
    except ValueError:
        return None


def availability(model: str, quota: dict) -> tuple[bool, str]:
    """Decide whether a model's provider has quota, with the reason."""
    provider = provider_for(model)
    if provider is None:
        return True, "no_quota_feed"
    summary = quota.get(provider)
    if not isinstance(summary, dict):
        return True, "unobserved"
    if summary.get("exhausted"):
        return False, "exhausted"
    used = summary.get("headline_used_percent")
    age = summary.get("age_seconds")
    fresh = age is None or age <= max_quota_age_seconds()
    if isinstance(used, (int, float)) and used >= exhausted_percent():
        if fresh:
            return False, f"used_percent {used:g}"
        return True, f"stale_observation age {age:g}"
    return True, "available"


def quota_summary() -> dict:
    """Read the broker quota summary, treating any failure as unobserved."""
    try:
        from agent_sessions.provider_quota import fetch_provider_quota_sync, summarise

        fetched = fetch_provider_quota_sync()
        providers = fetched.get("providers", {}) if isinstance(fetched, dict) else {}
        return summarise(providers if isinstance(providers, dict) else {})
    # nosemgrep: no-broad-except-swallow
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider quota unavailable for model selection: %s", exc)
        return {}


def select_model(role: str, policy: dict, *, quota: dict | None = None) -> dict:
    """Pick the first pool member with quota, falling back to the preferred model."""
    pool = pool_for(role, policy)
    preferred = pool[0]
    if quota is None:
        quota = quota_summary()
    skipped: list[dict] = []
    for model in pool:
        ok, reason = availability(model, quota)
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
