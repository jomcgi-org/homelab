"""Operational health components that drive homelab alerting.

Honeycomb's free plan allows one trigger, and it is spent on the public
jomcgi.dev /health composite. Everything else that used to be specced as a
Honeycomb trigger (projects/platform/honeycomb/triggers/) is evaluated here
instead and reported two ways:

- as ADVISORY components of the private deep ``/api/health`` (see
  ``factory.module``), so they are visible under ``degraded`` without turning
  the response into a 503, and
- through ``factory.health_alerts``, the leader-only loop that posts a Discord
  message when one of them flips.

Why advisory rather than fatal: each check describes a downstream operational
condition (EmberVM capacity, model providers, the factory lane), not whether
this process can serve requests. The kubelet probes only hit ``/healthz``,
which runs no deep components, so neither tier would restart pods either way.
But the fatal set is what ``/api/health`` 503s on and what the
"deep health components unhealthy" warning log keys on. A capacity or quota
problem is not the monolith being down, and alerting for it now goes to Discord
through the transition loop. None of these checks is composed into the public
tier, so the one Honeycomb trigger on the public composite is unaffected.

Every check returns ``{"ok": bool, "detail": str}``, is cached for
``CACHE_TTL_S`` per process, is bounded by ``CHECK_TIMEOUT_S`` and never raises:
a check that cannot evaluate reports ``ok: True`` with ``status: "unknown"``
and says why in ``detail``, because "could not look" is not evidence of the
failure it watches for (the deep health SELECT 1 already covers a DB outage).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

CACHE_TTL_S = 60.0
CHECK_TIMEOUT_S = 10.0
DB_STATEMENT_TIMEOUT_MS = 5000

# embervm_capacity
CREATE_FAILURE_STREAK = timedelta(minutes=15)
BRICK_NAMESPACE_ENV = "EMBERVM_BRICK_NAMESPACE"
BRICK_LABEL_SELECTOR = "app.kubernetes.io/component=noded-brick"
BRICK_CLASS_LABEL = "embervm.jomcgi.dev/size-class"

# agent_turns and codex_quota_fresh
TURN_WINDOW = timedelta(minutes=60)
# At least this many undelivered attempts before a family with zero deliveries
# is unhealthy. One lone failed turn in an hour is routine (a guest 422, one
# provider error); the 2026-09 outages all produced two or more per hour with
# no success, and no healthy hour in the four days to 2026-09-30 did.
TURN_MIN_FAILED_ENV = "AGENT_TURNS_ALERT_MIN_FAILED"
TURN_MIN_FAILED_DEFAULT = 2
FAMILIES = {
    "codex": ("luna", "terra", "sol", "astra"),
    "claude": ("opus", "sonnet", "fable"),
}
CODEX_QUOTA_MAX_AGE = timedelta(minutes=60)

# factory_stuck
UNCERTAIN_MAX_AGE = timedelta(hours=2)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _minutes(delta: timedelta) -> str:
    return f"{max(0, int(delta.total_seconds() // 60))}m"


def _unknown(detail: str) -> dict:
    return {"ok": True, "status": "unknown", "detail": detail}


@dataclass
class _CachedCheck:
    """Per-process TTL cache, single-flight, time-bounded, never raising."""

    name: str
    compute: Callable[[], Awaitable[dict]]
    ttl_s: float = CACHE_TTL_S
    timeout_s: float = CHECK_TIMEOUT_S
    _value: tuple[float, dict] | None = None
    _lock: asyncio.Lock | None = field(default=None, repr=False)

    def reset(self) -> None:
        self._value = None
        self._lock = None

    async def __call__(self) -> dict:
        now = time.monotonic()
        if self._value is not None and now - self._value[0] < self.ttl_s:
            return self._value[1]
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = time.monotonic()
            if self._value is not None and now - self._value[0] < self.ttl_s:
                return self._value[1]
            try:  # nosemgrep: no-broad-except-swallow - a check must never raise
                result = await asyncio.wait_for(self.compute(), self.timeout_s)
                if not isinstance(result, dict) or "ok" not in result:
                    result = _unknown(f"check returned {type(result).__name__}")
            except asyncio.TimeoutError:
                logger.warning("health check %s timed out", self.name)
                result = _unknown(f"check timed out after {self.timeout_s:g}s")
            except Exception as exc:
                logger.warning("health check %s failed", self.name, exc_info=True)
                result = _unknown(f"check failed: {type(exc).__name__}: {exc}")
            self._value = (time.monotonic(), result)
            return result


def _db_session():
    from core.db import get_engine
    from sqlmodel import Session

    session = Session(get_engine())
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy import text

        session.execute(
            text(f"SET LOCAL statement_timeout = {DB_STATEMENT_TIMEOUT_MS}")
        )
    return session


# ---------------------------------------------------------------------------
# embervm_capacity


async def _brick_replicas() -> tuple[dict[str, int] | None, str]:
    """Desired replicas per brick class, or None with the reason it is unknown."""
    namespace = os.environ.get(BRICK_NAMESPACE_ENV, "").strip()
    if not namespace:
        return None, "brick namespace not configured"
    try:
        from kubernetes_asyncio import client, config

        config.load_incluster_config()
        async with client.ApiClient() as api:
            listed = await client.AppsV1Api(api).list_namespaced_deployment(
                namespace,
                label_selector=BRICK_LABEL_SELECTOR,
                _request_timeout=5,
            )
    except Exception as exc:  # noqa: BLE001 - unknown, not unhealthy
        return None, f"brick deployments unreadable ({type(exc).__name__})"
    replicas = {}
    for item in listed.items:
        labels = item.metadata.labels or {}
        size = labels.get(BRICK_CLASS_LABEL) or item.metadata.name
        replicas[size] = int(item.spec.replicas or 0)
    return replicas, ""


def _create_streak_sync() -> tuple[object | None, object | None]:
    from core.platform_probe import PlatformProbe

    from factory.execution import create_outcome

    with _db_session() as session:
        latest = session.get(PlatformProbe, create_outcome.LATEST)
        since = session.get(PlatformProbe, create_outcome.FAILING_SINCE)
        if latest is not None:
            session.expunge(latest)
        if since is not None:
            session.expunge(since)
        return latest, since


def evaluate_create_streak(latest, since, now: datetime) -> tuple[bool, str]:
    """(failing past the threshold, detail) from the two create-outcome rows.

    A streak counts only while its failures SPAN the threshold: a single failed
    create followed by no further attempts is not "failing continuously". Once
    the span passes 15 minutes it stays unhealthy until a create succeeds.
    """
    if latest is None:
        return False, "no session create recorded yet"
    checked_at = _utc(latest.checked_at)
    if latest.ok:
        return False, f"last session create ok {_minutes(now - checked_at)} ago"
    started = checked_at
    if since is not None:
        candidate = _utc(since.checked_at)
        last_ok = _utc(latest.last_ok_at) if latest.last_ok_at else None
        if candidate <= checked_at and (last_ok is None or candidate >= last_ok):
            started = candidate
    span = checked_at - started
    detail = (
        f"session creates failing since {started:%Y-%m-%d %H:%M} UTC "
        f"({_minutes(now - started)}; last failure {_minutes(now - checked_at)} "
        f"ago: {latest.detail or 'no detail'})"
    )
    return span > CREATE_FAILURE_STREAK, detail


async def _embervm_capacity() -> dict:
    replicas, brick_note = await _brick_replicas()
    try:  # nosemgrep: no-broad-except-swallow - reported in detail
        latest, since = await asyncio.to_thread(_create_streak_sync)
        streak_failing, streak_detail = evaluate_create_streak(latest, since, _now())
    except Exception as exc:  # noqa: BLE001
        streak_failing, streak_detail = False, f"create outcomes unreadable ({exc})"

    problems = []
    if replicas is not None:
        classes = ", ".join(f"{k}={v}" for k, v in sorted(replicas.items()))
        if not replicas:
            problems.append("no brick deployments found")
            brick_note = "no brick deployments found"
        elif sum(replicas.values()) == 0:
            problems.append(f"every brick class at 0 replicas ({classes})")
            brick_note = problems[-1]
        else:
            brick_note = f"brick replicas {classes}"
    if streak_failing:
        problems.append(streak_detail)
    if problems:
        return {"ok": False, "detail": "; ".join(problems)}
    return {"ok": True, "detail": f"{brick_note}; {streak_detail}"}


# ---------------------------------------------------------------------------
# agent_turns and codex_quota_fresh


def _family(model: str | None) -> str | None:
    if model is None:
        return "claude"  # a session with no model runs the Claude default
    for family, models in FAMILIES.items():
        if model in models:
            return family
    return None


@dataclass
class FamilyTurns:
    attempted: int = 0
    delivered: int = 0
    last_failure: str = ""


def _turn_rows_sync(since: datetime) -> list[tuple]:
    from sqlmodel import select

    from factory.execution.models import AgentSession, AgentTurn

    with _db_session() as session:
        return list(
            session.exec(
                select(
                    AgentTurn.model,
                    AgentSession.model,
                    AgentTurn.terminal_reason,
                    AgentTurn.stop_reason,
                    AgentTurn.created_at,
                )
                .join(AgentSession, AgentSession.id == AgentTurn.session_id)
                .where(AgentTurn.created_at >= since)
                .order_by(AgentTurn.created_at)
            ).all()
        )


def summarise_turns(rows: list[tuple]) -> dict[str, FamilyTurns]:
    """Per-family attempted and delivered turn counts.

    Interrupted turns (a rollout or drain) are superseded by their re-dispatch
    and a turn cancelled before dispatch never reached a model, so neither is
    an attempt. Delivered means the turn ended with a clean terminal reason.
    """
    from factory.execution.constants import (
        CLEAN_TERMINAL_REASONS,
        INTERRUPTED_TERMINAL_REASONS,
    )

    stats = {family: FamilyTurns() for family in FAMILIES}
    for turn_model, session_model, terminal, stop, _created in rows:
        family = _family(turn_model or session_model)
        if family is None:
            continue
        if terminal in INTERRUPTED_TERMINAL_REASONS:
            continue
        if stop == "cancelled_before_dispatch":
            continue
        entry = stats[family]
        entry.attempted += 1
        if terminal in CLEAN_TERMINAL_REASONS:
            entry.delivered += 1
        else:
            entry.last_failure = "/".join(p for p in (terminal, stop) if p) or "none"
    return stats


_turns_snapshot: tuple[float, dict[str, FamilyTurns]] | None = None


async def _turn_stats() -> dict[str, FamilyTurns]:
    """Shared by agent_turns and codex_quota_fresh; one query per TTL."""
    global _turns_snapshot
    now = time.monotonic()
    if _turns_snapshot is not None and now - _turns_snapshot[0] < CACHE_TTL_S:
        return _turns_snapshot[1]
    rows = await asyncio.to_thread(_turn_rows_sync, _now() - TURN_WINDOW)
    stats = summarise_turns(rows)
    _turns_snapshot = (time.monotonic(), stats)
    return stats


def _min_failed() -> int:
    raw = os.environ.get(TURN_MIN_FAILED_ENV, "")
    try:
        return max(1, int(raw)) if raw else TURN_MIN_FAILED_DEFAULT
    except ValueError:
        return TURN_MIN_FAILED_DEFAULT


def evaluate_turns(stats: dict[str, FamilyTurns], min_failed: int) -> dict:
    window = _minutes(TURN_WINDOW)
    failing, parts = [], []
    for family in FAMILIES:
        entry = stats.get(family, FamilyTurns())
        part = f"{family} {entry.delivered}/{entry.attempted} delivered"
        if entry.delivered == 0 and entry.attempted >= min_failed:
            failing.append(family)
            part += f" (last: {entry.last_failure})"
        parts.append(part)
    summary = f"{'; '.join(parts)} in {window}"
    if failing:
        return {
            "ok": False,
            "detail": f"no {'/'.join(failing)} turn delivered in {window}: {summary}",
        }
    return {"ok": True, "detail": summary}


async def _agent_turns() -> dict:
    return evaluate_turns(await _turn_stats(), _min_failed())


def evaluate_codex_quota(quota: dict, codex_demand: int) -> dict:
    """Stale Codex quota matters only when Codex work was attempted.

    The broker observes quota from the headers of Codex requests passing
    through it, so an idle night ages the observation without anything being
    wrong. Requiring Codex demand (an attempted Codex turn in the last hour)
    keeps idle nights quiet. The cost: a fault that stops Codex work from even
    being attempted is not caught here; agent_turns and embervm_capacity cover
    that side.
    """
    limit = _minutes(CODEX_QUOTA_MAX_AGE)
    demand = f"{codex_demand} Codex turn(s) attempted in {_minutes(TURN_WINDOW)}"
    if not quota.get("available", False):
        reason = quota.get("reason", "broker unavailable")
        if codex_demand:
            return {
                "ok": False,
                "detail": f"quota broker unavailable ({reason}); {demand}",
            }
        return {
            "ok": True,
            "detail": f"quota broker unavailable ({reason}); no Codex demand",
        }
    codex = (quota.get("providers") or {}).get("codex")
    age = codex.get("age_seconds") if isinstance(codex, dict) else None
    observed = isinstance(codex, dict) and codex.get("observed") is True
    if not observed or not isinstance(age, (int, float)) or isinstance(age, bool):
        detail = "no Codex quota observation at the broker"
        if codex_demand:
            return {"ok": False, "detail": f"{detail}; {demand}"}
        return {"ok": True, "detail": f"{detail}; no Codex demand"}
    age_text = _minutes(timedelta(seconds=float(age)))
    if age > CODEX_QUOTA_MAX_AGE.total_seconds():
        if codex_demand:
            return {
                "ok": False,
                "detail": f"Codex quota observed {age_text} ago (limit {limit}); {demand}",
            }
        return {
            "ok": True,
            "detail": f"Codex quota observed {age_text} ago; no Codex demand",
        }
    return {"ok": True, "detail": f"Codex quota observed {age_text} ago"}


async def _codex_quota_fresh() -> dict:
    from factory.execution.provider_quota import fetch_provider_quota

    quota = await fetch_provider_quota()
    stats = await _turn_stats()
    return evaluate_codex_quota(quota, stats["codex"].attempted)


# ---------------------------------------------------------------------------
# factory_stuck


def _factory_rows_sync() -> tuple[object | None, list, list]:
    from sqlmodel import select

    from factory.orchestration.factory_models import FactoryControl, FactoryReceipt

    with _db_session() as session:
        control = session.get(FactoryControl, "factory")
        policy_json = control.policy_json if control is not None else None
        uncertain = list(
            session.exec(
                select(
                    FactoryReceipt.id,
                    FactoryReceipt.issue_number,
                    FactoryReceipt.updated_at,
                ).where(FactoryReceipt.state == "uncertain")
            ).all()
        )
        queued = list(
            session.exec(
                select(
                    FactoryReceipt.id,
                    FactoryReceipt.issue_number,
                    FactoryReceipt.actor,
                    FactoryReceipt.repo,
                    FactoryReceipt.generation,
                ).where(FactoryReceipt.state == "queued")
            ).all()
        )
        return policy_json, uncertain, queued


def evaluate_factory(
    policy_json: str | None, uncertain: list, queued: list, now: datetime
) -> dict:
    """Uncertain receipts older than 2h, and queued receipts admit_next never takes.

    Uncertain age is measured from the receipt's updated_at, which is when it
    last changed state. A queued receipt is flagged when it is at the live
    policy's repo and generation (so admit_next would consider it) but
    admission_eligible says no: the #6483 trap, where it holds lane room and
    blocks intake forever. Intake receipts while intake is switched off are
    left out: that is an operator's deliberate pause, not a stuck lane.
    Receipts from an older generation are left out too, since a generation
    bump strands them by design.
    """
    from factory.orchestration.factory_controls import INTAKE_ACTOR, intake_policy
    from factory.orchestration.factory_intake import admission_eligible

    problems = []
    stale = sorted(
        (
            (now - _utc(updated), rid, issue)
            for rid, issue, updated in uncertain
            if now - _utc(updated) > UNCERTAIN_MAX_AGE
        ),
        reverse=True,
    )
    if stale:
        listed = ", ".join(
            f"receipt {rid} (#{issue}) {_minutes(age)}" for age, rid, issue in stale[:5]
        )
        more = f" and {len(stale) - 5} more" if len(stale) > 5 else ""
        problems.append(
            f"{len(stale)} receipt(s) uncertain over {_minutes(UNCERTAIN_MAX_AGE)}: "
            f"{listed}{more}"
        )

    ineligible = []
    policy_note = ""
    try:
        policy = json.loads(policy_json) if policy_json else None
    except ValueError:
        policy = None
    if not isinstance(policy, dict):
        policy_note = "; no factory policy to check queued receipts against"
    else:
        try:
            intake_on = bool(intake_policy(policy).get("enabled"))
        except Exception:  # noqa: BLE001 - malformed intake block reads as off
            intake_on = False
        for rid, issue, actor, repo, generation in queued:
            if repo != policy.get("repo") or generation != policy.get("generation"):
                continue
            if actor == INTAKE_ACTOR and not intake_on:
                continue
            try:
                eligible = admission_eligible(policy, issue, actor)
            except Exception:  # noqa: BLE001 - a policy it cannot read admits nothing
                eligible = False
            if not eligible:
                ineligible.append(f"receipt {rid} (#{issue}, actor {actor})")
    if ineligible:
        problems.append(
            f"{len(ineligible)} queued receipt(s) not admission-eligible: "
            + ", ".join(ineligible[:5])
            + (f" and {len(ineligible) - 5} more" if len(ineligible) > 5 else "")
        )
    if problems:
        return {"ok": False, "detail": "; ".join(problems)}
    return {
        "ok": True,
        "detail": (
            f"{len(uncertain)} uncertain (none over "
            f"{_minutes(UNCERTAIN_MAX_AGE)}), {len(queued)} queued{policy_note}"
        ),
    }


async def _factory_stuck() -> dict:
    policy_json, uncertain, queued = await asyncio.to_thread(_factory_rows_sync)
    return evaluate_factory(policy_json, uncertain, queued, _now())


# ---------------------------------------------------------------------------

embervm_capacity_health = _CachedCheck("embervm_capacity", _embervm_capacity)
agent_turns_health = _CachedCheck("agent_turns", _agent_turns)
codex_quota_fresh_health = _CachedCheck("codex_quota_fresh", _codex_quota_fresh)
factory_stuck_health = _CachedCheck("factory_stuck", _factory_stuck)

CHECKS: dict[str, _CachedCheck] = {
    check.name: check
    for check in (
        embervm_capacity_health,
        agent_turns_health,
        codex_quota_fresh_health,
        factory_stuck_health,
    )
}


def reset_caches() -> None:
    """Clear process-local caches (tests)."""
    global _turns_snapshot
    _turns_snapshot = None
    for check in CHECKS.values():
        check.reset()
