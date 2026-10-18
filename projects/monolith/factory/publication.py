"""Build the public factory snapshot from the private factory tables.

The jomcgi.dev factory pages cannot read the factory. public_reader has no
grant on the swarm or agent_sessions schemas, and the public image is pruned of
private execution and publication code, so there is nothing on that side that could assemble
a board even if the rows were reachable. This module is the private half of the
snapshot pattern that answers that: it reads the real tables with the real
code, shapes four payload kinds, and writes them to public_api tables that
factory/public_view.py serves with plain SQL.

Four payload kinds, because the pages differ by an order of magnitude in
size:

* the activity payload is the board (in flight, queued, recent) and is read on
  every visit,
* one task payload is that task's walkthrough (brief, plan nodes, attempts, and
  a digest of every turn),
* one session payload is the full record of one attempt, every turn with its
  prompt, result, diff and rationale.
* one work-item payload is the safe GitHub-origin record and public-to-public
  edges, without receipts, events, escalation data or execution identifiers.

Prompts, results and diffs go out verbatim: that is the point of the pages, and
the repo owner approved it. Identity does not. ``actor``, ``triggered_by``,
``ember_session_id``, session titles, voice summaries and artifact bodies are
excluded here rather than filtered downstream, so a field has to be added on
purpose to become public.

Every shaping helper takes plain dicts and imports nothing from ``swarm``, so
the unit tests run without a database and without linking the swarm package.
Only ``build_public_snapshot`` and ``write_public_snapshot`` touch the engine.
"""

from __future__ import annotations

import json
import logging
import math
import re
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, Integer, String, bindparam
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select, text

from core.db import row_value
from factory.utils import sanitize_payload

logger = logging.getLogger(__name__)

# One turn's diff, decompressed. 256 KiB is the same ceiling the guest shim
# uses for a progress payload, and it keeps one session payload bounded even
# when a turn rewrote half the repo.
DIFF_LIMIT = 262144
# The shim records one activity per tool call. A long turn can run hundreds;
# the tail is the interesting part, so keep the last 300.
ACTIVITY_LIMIT = 300
BRIEF_PARAGRAPHS = 6
# factory.orchestration.factory_controls.DEFAULT_MAX_REVIEW_ROUNDS, duplicated rather than
# imported so this module stays swarm-free for the unit tests. A policy written
# before the cap existed carries no value and the engine assumes this one.
DEFAULT_MAX_REVIEW_ROUNDS = 2

POLICY_KEYS = (
    "generation",
    "max_tasks",
    "conductor_model",
    "worker_model",
    "reviewer_model",
    "max_task_turns_hard",
    "max_parallel_nodes",
    "task_budget_usd",
    "max_attempts",
)

# The engine names its own review rounds correct_<n>; the node's `kind` is
# "work" like any other implementing node, so the round count reads the key.
_CORRECTION_NODE = re.compile(r"^correct_[0-9]+$")

# The board word for a receipt state, matching RECEIPT_STATE_WORD in
# frontend/src/routes/private/agents/factory/factory-view.js so the public
# board and the private board call the same thing by the same name.
RECEIPT_STATE_WORD = {
    "queued": "queued",
    "admitted": "in flight",
    "uncertain": "uncertain",
    "escalated": "escalated",
    "succeeded": "landed",
    "failed": "failed",
    "cancelled": "cancelled",
}
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "escalated")

# The phase word for a task that never got a node far enough to name one. A
# failed task with no node that ran was escalated out rather than finished, so
# it reads as escalated rather than borrowing the board word.
_PHASE_FALLBACK = {
    "queued": "queued",
    "admitted": "queued",
    "succeeded": "done",
    "uncertain": "escalated",
    "escalated": "escalated",
    "failed": "escalated",
    "cancelled": "cancelled",
}

_ACTIVITY_KEYS = ("type", "file_path", "command", "name")
# The same aliases public_api.agent_activity_daily folds together: different
# providers name the cache-read counter differently.
_CACHE_READ_KEYS = (
    "cache_read_tokens",
    "cache_read_input_tokens",
    "cached_input_tokens",
)

# Stop events carry an actor and a request key on the private board. Neither is
# public; the public page shows what happened and whether a human was needed.
_STOP_EVENT_KEYS = ("action", "reason", "intervention_required")


def _iso(value) -> str | None:
    """ISO 8601 in UTC, treating a naive timestamp as UTC like the board does."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# A usage report can survive an idle subscription for hours. Keep that last
# measurement visible, but never compare it with today's clock as fresh pace.
QUOTA_STALE_AFTER_SECONDS = 900


def _quota_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _quota_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _quota_windows(
    views: list[dict], now: datetime, fetched: datetime | None
) -> tuple[list[dict], int]:
    groups: dict[float, list[dict]] = {}
    unknown = 0
    for view in views:
        if view.get("observed") is not True:
            continue
        windows = view.get("windows")
        if not isinstance(windows, list):
            continue
        # One account contributes once per duration. Ambiguous duplicate limits
        # are omitted, never summed into a fictitious larger subscription.
        account: dict[float, list[dict]] = {}
        for window in windows:
            if not isinstance(window, dict):
                unknown += 1
                continue
            minutes = _quota_number(window.get("window_minutes"))
            if minutes is None or not 0 < minutes <= 525600:
                unknown += 1
                continue
            account.setdefault(minutes * 60, []).append(window)
        age = _quota_number(view.get("age_seconds"))
        observed_at = None
        if age is not None and 0 <= age <= 315360000 and fetched is not None:
            observed_at = fetched - timedelta(seconds=age)
        for seconds, candidates in account.items():
            samples = groups.setdefault(seconds, [])
            if len(candidates) != 1:
                continue
            window = candidates[0]
            used = _quota_number(window.get("used_percent"))
            reset = _quota_time(window.get("resets_at"))
            if (
                used is None
                or not 0 <= used <= 100
                or reset is None
                or window.get("expired") not in (None, False)
                or not 0 < (reset - now).total_seconds() <= seconds
            ):
                continue
            samples.append({"used": used, "reset": reset, "observed_at": observed_at})
    result = []
    for seconds, samples in sorted(groups.items()):
        count = len(samples)
        resets = [sample["reset"] for sample in samples]
        observed = [sample["observed_at"] for sample in samples]
        fresh = sum(
            at is not None
            and 0 <= (now - at).total_seconds() <= QUOTA_STALE_AFTER_SECONDS
            for at in observed
        )
        used = sum(sample["used"] for sample in samples) / count if count else None
        remaining = (
            sum((reset - now).total_seconds() for reset in resets) / count
            if count
            else None
        )
        result.append(
            {
                "window_seconds": seconds,
                "reported_accounts": count,
                "fresh_accounts": fresh,
                "used_percent": used,
                "remaining_seconds": remaining,
                "elapsed_percent": 100 * (1 - remaining / seconds)
                if remaining is not None
                else None,
                "next_reset_at": _iso(min(resets)) if resets else None,
                "last_reset_at": _iso(max(resets)) if resets else None,
                "oldest_observed_at": _iso(min(observed))
                if observed and all(observed)
                else None,
            }
        )
    return result, unknown


_QUOTA_PACING_STATES = (
    "use_more",
    "on_pace",
    "conserve",
    "wait_for_reset",
    "unknown",
    "stale",
    "awaiting_report",
)


def _quota_pacing_view(
    view: dict, now: datetime, fetched: datetime | None
) -> dict | None:
    """Validate advisory broker fields before exposing an anonymous aggregate."""
    pacing = view.get("pacing")
    if not isinstance(pacing, dict) or view.get("observed") is not True:
        return None
    state = pacing.get("state")
    evaluated = _quota_time(pacing.get("evaluated_at"))
    age = _quota_number(view.get("age_seconds"))
    if (
        state not in _QUOTA_PACING_STATES
        or evaluated is None
        or fetched is None
        or age is None
        or not 0 <= age <= 315360000
        or not 0 <= (now - evaluated).total_seconds() <= QUOTA_STALE_AFTER_SECONDS
    ):
        return None
    observed = fetched - timedelta(seconds=age)
    if not 0 <= (now - observed).total_seconds() <= QUOTA_STALE_AFTER_SECONDS:
        return None
    result = {"state": state, "evaluated_at": evaluated, "observed_at": observed}
    windows = view.get("windows")
    windows = windows if isinstance(windows, list) else []
    if state == "wait_for_reset":
        blocked = _quota_time(pacing.get("blocked_until"))
        exhausted = [
            _quota_time(window.get("resets_at"))
            for window in windows
            if isinstance(window, dict) and window.get("used_percent") == 100
        ]
        active = [reset for reset in exhausted if reset is not None and reset > now]
        if blocked is not None and active and all(exhausted) and blocked == max(active):
            result["blocked_until"] = blocked
    if state in ("unknown", "stale", "awaiting_report"):
        return result
    minutes = _quota_number(pacing.get("budget_window_minutes"))
    reset = _quota_time(pacing.get("budget_resets_at"))
    used = _quota_number(pacing.get("budget_used_percent"))
    remaining = _quota_number(pacing.get("budget_remaining_percent"))
    daily = _quota_number(pacing.get("budget_per_day_percent"))
    target = _quota_number(pacing.get("target_used_percent"))
    delta = _quota_number(pacing.get("pace_delta_percent"))
    reported_seconds = _quota_number(pacing.get("budget_remaining_seconds"))
    budget = [
        window
        for window in windows
        if isinstance(window, dict) and window.get("window_minutes") == minutes
    ]
    if (
        minutes is None
        or reset is None
        or used is None
        or remaining is None
        or daily is None
        or target is None
        or delta is None
        or reported_seconds is None
    ):
        return result if state == "wait_for_reset" else None
    seconds_left = (reset - evaluated).total_seconds()
    if (
        not 0 < minutes <= 525600
        or not 0 <= used <= 100
        or not 0 <= remaining <= 100
        or not 0 < seconds_left <= minutes * 60
        or reset <= now
        or not math.isclose(reported_seconds, seconds_left, abs_tol=0.01)
        or not math.isclose(
            target, 100 * (1 - seconds_left / (minutes * 60)), abs_tol=0.01
        )
        or not math.isclose(delta, used - target, abs_tol=0.01)
        or not math.isclose(used + remaining, 100, abs_tol=0.01)
        or not math.isclose(daily, remaining * 86400 / seconds_left, abs_tol=0.01)
        or len(budget) != 1
        or budget[0].get("used_percent") != used
        or _quota_time(budget[0].get("resets_at")) != reset
        or budget[0].get("expired") not in (None, False)
        or any(
            (_quota_number(window.get("window_minutes")) or 0) > minutes
            for window in windows
            if isinstance(window, dict)
        )
    ):
        return None
    result.update(
        budget_window_seconds=minutes * 60,
        budget_remaining_percent=remaining,
        budget_per_day_percent=daily,
        budget_resets_at=reset,
    )
    short = _quota_number(pacing.get("short_window_minutes"))
    short_reset = _quota_time(pacing.get("short_resets_at"))
    short_windows = [
        window
        for window in windows
        if isinstance(window, dict) and window.get("window_minutes") == short
    ]
    if (
        short is not None
        and 1 <= short < minutes
        and short_reset is not None
        and short_reset > now
        and len(short_windows) == 1
        and short_windows[0].get("expired") in (None, False)
        and _quota_time(short_windows[0].get("resets_at")) == short_reset
        and (short_reset - observed).total_seconds() <= short * 60 + 1
    ):
        result.update(short_window_seconds=short * 60, short_resets_at=short_reset)
    return result


def _quota_pacing(
    views: list[dict], now: datetime, fetched: datetime | None, inventory: bool
) -> dict | None:
    # Absence remains absence during rollout or when reading an old snapshot.
    if not any(isinstance(view.get("pacing"), dict) for view in views):
        return None
    samples = [_quota_pacing_view(view, now, fetched) for view in views]
    valid = [sample for sample in samples if sample is not None]
    counts: dict[str, int] = {}
    for sample in samples:
        state = sample["state"] if sample else "unknown"
        counts[state] = counts.get(state, 0) + 1
    result = {
        "reported_accounts": len(valid),
        "complete": False,
        "state_counts": counts,
        "evaluated_at": _iso(min(sample["evaluated_at"] for sample in valid))
        if valid
        else None,
        "oldest_observed_at": _iso(min(sample["observed_at"] for sample in valid))
        if valid
        else None,
    }
    resets = [
        reset
        for view in views
        for window in (view["windows"] if isinstance(view.get("windows"), list) else [])
        if isinstance(window, dict)
        and (reset := _quota_time(window.get("resets_at"))) is not None
    ]
    result["valid_until"] = _iso(min(resets)) if resets else None
    waiting = [sample for sample in valid if sample["state"] == "wait_for_reset"]
    if waiting and all("blocked_until" in sample for sample in waiting):
        result.update(
            blocked_next_reset_at=_iso(
                min(sample["blocked_until"] for sample in waiting)
            ),
            blocked_last_reset_at=_iso(
                max(sample["blocked_until"] for sample in waiting)
            ),
        )
    budgets = [sample for sample in valid if "budget_window_seconds" in sample]
    if (
        not inventory
        or len(budgets) != len(views)
        or len({sample["budget_window_seconds"] for sample in budgets}) != 1
    ):
        return result
    result.update(
        complete=True,
        budget_window_seconds=budgets[0]["budget_window_seconds"],
        budget_remaining_percent=sum(
            sample["budget_remaining_percent"] for sample in budgets
        )
        / len(budgets),
        budget_per_day_percent=sum(
            sample["budget_per_day_percent"] for sample in budgets
        )
        / len(budgets),
    )
    # Preserve the reported reset range across accounts without predicting later cycles.
    if (
        all("short_resets_at" in sample for sample in budgets)
        and len({sample["short_window_seconds"] for sample in budgets}) == 1
    ):
        result.update(
            short_window_seconds=budgets[0]["short_window_seconds"],
            short_next_reset_at=_iso(
                min(sample["short_resets_at"] for sample in budgets)
            ),
            short_last_reset_at=_iso(
                max(sample["short_resets_at"] for sample in budgets)
            ),
        )
    return result


def shape_quota(payload: dict | None, now: datetime) -> dict:
    """Public allowlist: account means and coverage, without account identity.

    Percentages have unknown token capacities. An equal-account mean is the
    only aggregate justified by these reports. Provider rollups are a fallback
    observation with unknown coverage, and never another account in the mean.
    """
    payload = payload if isinstance(payload, dict) else {}
    available = payload.get("available") is True
    fetched = _quota_time(payload.get("fetched_at")) if available else None
    if fetched is not None and fetched > now:
        fetched = None
    grants = payload.get("grants")
    valid = isinstance(grants, dict) and payload.get("grants_valid") is True
    inventory = payload.get("grants_complete") is True and valid
    if not isinstance(grants, dict):
        grants = {}
    # A malformed entry with no provider could hide an account of either kind.
    inventory = inventory and all(
        isinstance(view, dict) and isinstance(view.get("provider"), str)
        for view in grants.values()
    )
    raw_providers = payload.get("providers")
    raw_providers = raw_providers if isinstance(raw_providers, dict) else {}
    providers = []
    for provider in ("codex", "claude"):
        accounts = (
            [
                view
                for view in grants.values()
                if isinstance(view, dict) and view.get("provider") == provider
            ]
            if available
            else []
        )
        fallback = raw_providers.get(provider)
        views = accounts or (
            [fallback] if available and isinstance(fallback, dict) else []
        )
        windows, unknown = _quota_windows(views, now, fetched)
        for window in windows:
            window["complete"] = (
                bool(accounts)
                and inventory
                and window["reported_accounts"] == len(accounts)
            )
            window["pace_delta_percent"] = (
                window["used_percent"] - window["elapsed_percent"]
                if window["complete"] and window["fresh_accounts"] == len(accounts)
                else None
            )
        providers.append(
            {
                "provider": provider,
                "source": "accounts" if accounts else "provider",
                "configured_accounts": len(accounts) if accounts else None,
                "inventory_complete": bool(accounts) and inventory,
                "unknown_windows": unknown,
                "windows": windows,
                "pacing": _quota_pacing(
                    views, now, fetched, inventory if accounts else True
                ),
            }
        )
    return {
        "available": available,
        "sampled_at": _iso(now),
        "stale_after_seconds": QUOTA_STALE_AFTER_SECONDS,
        "providers": providers,
    }


QUOTA_HISTORY_DAYS = 8
QUOTA_HISTORY_BUCKET_SECONDS = 900
QUOTA_RESET_JITTER_SECONDS = 60
_QUOTA_HISTORY_STATUSES = ("complete", "observed", "partial", "stale", "unavailable")


def _quota_history_point(raw: object, now: datetime, seconds: float) -> dict | None:
    """Rebuild even previously published points, never trust stored JSON fields."""
    if not isinstance(raw, dict):
        return None
    sampled = _quota_time(raw.get("sampled_at"))
    if (
        sampled is None
        or not now - timedelta(days=QUOTA_HISTORY_DAYS) <= sampled <= now
    ):
        return None
    source, status = raw.get("source"), raw.get("status")
    if source not in ("accounts", "provider") or status not in _QUOTA_HISTORY_STATUSES:
        return None
    observed = _quota_time(raw.get("observed_at"))
    first, last = (
        _quota_time(raw.get(key)) for key in ("next_reset_at", "last_reset_at")
    )
    counts = []
    for key in ("reported_accounts", "configured_accounts"):
        value = raw.get(key)
        counts.append(value if type(value) is int and 0 <= value <= 10000 else None)
    reported, configured = counts
    complete = (
        raw.get("complete") is True
        and source == "accounts"
        and configured == reported
        and bool(reported)
    )
    fresh = (
        raw.get("fresh") is True
        and observed is not None
        and 0 <= (sampled - observed).total_seconds() <= QUOTA_STALE_AFTER_SECONDS
    )
    used, elapsed = (
        _quota_number(raw.get(key)) for key in ("used_percent", "elapsed_percent")
    )
    valid = (
        fresh
        and first is not None
        and last is not None
        and 0
        < (first - sampled).total_seconds()
        <= (last - sampled).total_seconds()
        <= seconds
        and used is not None
        and 0 <= used <= 100
        and elapsed is not None
        and 0 <= elapsed <= 100
        and (
            (status == "complete" and complete)
            or (status == "observed" and source == "provider")
        )
    )
    if status in ("complete", "observed") and not valid:
        status = "stale" if not fresh else "partial"
    return {
        "sampled_at": _iso(sampled),
        "observed_at": _iso(observed),
        "used_percent": used if valid else None,
        "elapsed_percent": elapsed if valid else None,
        "next_reset_at": _iso(first),
        "last_reset_at": _iso(last),
        "reported_accounts": reported,
        "configured_accounts": configured,
        "complete": bool(complete),
        "fresh": fresh,
        "source": source,
        "status": status,
        "break_before": raw.get("break_before") is True,
        "reset_before": raw.get("reset_before") is True,
    }


def _quota_history_append(
    points: list[dict], point: dict, window_seconds: float
) -> list[dict]:
    if points:
        prior = points[-1]
        scope_changed = any(
            prior[key] != point[key]
            for key in (
                "source",
                "configured_accounts",
                "reported_accounts",
                "complete",
                "status",
            )
        )
        # Provider reset clocks can wobble between observations. Keep that
        # jitter from inventing a reset or a gap, but cap the allowance at 1%
        # of the window so a genuine short-window rollover remains visible.
        reset_tolerance = min(QUOTA_RESET_JITTER_SECONDS, window_seconds / 100)
        reset_pairs = tuple(
            (_quota_time(prior[key]), _quota_time(point[key]))
            for key in ("next_reset_at", "last_reset_at")
        )
        reset_changed = any(
            old != new
            and (
                old is None
                or new is None
                or abs((new - old).total_seconds()) > reset_tolerance
            )
            for old, new in reset_pairs
        )
        old_observed, observed = (_quota_time(p["observed_at"]) for p in (prior, point))
        # Broker ages are rounded and can jitter slightly between reads. An
        # unchanged cached report must not become another measured data point.
        same_observation = old_observed == observed or (
            old_observed is not None
            and observed is not None
            and abs((observed - old_observed).total_seconds()) <= 2
        )
        if (
            not scope_changed
            and not reset_changed
            and prior["used_percent"] == point["used_percent"]
            and (same_observation or point["used_percent"] is None)
        ):
            return points
        # Losing the earliest-reset account also advances the reset range.
        # Only comparable measured populations support a reset annotation.
        advanced_reset = (
            not scope_changed
            and prior["used_percent"] is not None
            and point["used_percent"] is not None
            and any(
                old is not None
                and new is not None
                and (new - old).total_seconds() > reset_tolerance
                for old, new in reset_pairs
            )
        )
        usage_drop = (
            prior["used_percent"] is not None
            and point["used_percent"] is not None
            and point["used_percent"] < prior["used_percent"]
        )
        gap = (
            _quota_time(point["sampled_at"]) - _quota_time(prior["sampled_at"])
        ).total_seconds() > 2 * QUOTA_HISTORY_BUCKET_SECONDS
        point["reset_before"] = point["reset_before"] or advanced_reset
        point["break_before"] = (
            point["break_before"] or scope_changed or reset_changed or usage_drop or gap
        )
    return [*points, point]


def _quota_history_compact(points: list[dict]) -> list[dict]:
    """First and latest real observation per bucket, carrying skipped breaks."""
    buckets: dict[int, list[dict]] = {}
    for point in sorted(points, key=lambda value: value["sampled_at"]):
        bucket = (
            int(_quota_time(point["sampled_at"]).timestamp())
            // QUOTA_HISTORY_BUCKET_SECONDS
        )
        rows = buckets.setdefault(bucket, [])
        if not rows or rows[-1]["sampled_at"] != point["sampled_at"]:
            rows.append(point)
        else:
            rows[-1] = point
    compacted = []
    for rows in buckets.values():
        compacted.append(rows[0])
        if len(rows) > 1:
            last = dict(rows[-1])
            last["break_before"] = any(row["break_before"] for row in rows[1:])
            last["reset_before"] = any(row["reset_before"] for row in rows[1:])
            compacted.append(last)
    return compacted


def shape_quota_history(quota: dict, previous: object) -> list[dict]:
    """Eight days of measured quota burn, kept in the existing board singleton.

    Missing, stale and partial account inventories insert gaps. A provider-only
    reading is useful history but remains explicitly coverage-unknown. No
    synthetic beginning, interpolated usage or cached-report heartbeat is added.
    """
    now = _quota_time(quota.get("sampled_at"))
    if now is None:
        return []
    groups: dict[tuple[str, float], list[dict]] = {}
    for group in previous if isinstance(previous, list) else []:
        if not isinstance(group, dict) or group.get("provider") not in (
            "codex",
            "claude",
        ):
            continue
        seconds = _quota_number(group.get("window_seconds"))
        rows = group.get("points")
        if seconds is None or not 0 < seconds <= 31536000 or not isinstance(rows, list):
            continue
        points = [
            point
            for raw in rows
            if (point := _quota_history_point(raw, now, seconds)) is not None
        ]
        if points:
            groups[(group["provider"], seconds)] = _quota_history_compact(points)
    current = {}
    for provider in quota["providers"]:
        for window in provider["windows"]:
            fresh = (
                window["reported_accounts"] > 0
                and window["fresh_accounts"] == window["reported_accounts"]
            )
            status = (
                "stale"
                if not fresh
                else (
                    "complete"
                    if window["complete"]
                    else "observed"
                    if provider["source"] == "provider"
                    else "partial"
                )
            )
            point = _quota_history_point(
                {
                    **window,
                    "sampled_at": quota["sampled_at"],
                    "observed_at": window["oldest_observed_at"],
                    "source": provider["source"],
                    "configured_accounts": provider["configured_accounts"],
                    "fresh": fresh,
                    "status": status,
                },
                now,
                window["window_seconds"],
            )
            if point is not None:
                current[(provider["provider"], window["window_seconds"])] = point
    for key in groups.keys() | current.keys():
        points = groups.get(key, [])
        point = current.get(key)
        if point is None:
            # Retain a real outage marker even when the broker omitted every
            # window. The last known coverage is context, never carried usage.
            point = {
                **points[-1],
                "sampled_at": quota["sampled_at"],
                "used_percent": None,
                "elapsed_percent": None,
                "fresh": False,
                "status": "unavailable" if not quota["available"] else "partial",
                "break_before": True,
                "reset_before": False,
            }
        groups[key] = _quota_history_compact(
            _quota_history_append(points, point, key[1])
        )
    return [
        {"provider": provider, "window_seconds": seconds, "points": points}
        for (provider, seconds), points in sorted(groups.items())
    ]


def flatten_max_tasks(value: object) -> int | None:
    """The delivery lane's concurrency as one integer.

    A stored policy carries ``max_tasks`` either as a bare integer (older
    policies) or as a per-lane dict ``{"delivery": n, "advisory": m}``
    (``factory.orchestration.factory_controls._validate_max_tasks``). The public pages talk
    about delivery tasks only, so the dict collapses to its delivery count.
    """
    if isinstance(value, dict):
        value = value.get("delivery")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def shape_policy(board_policy: dict | None, raw_policy: dict | None = None) -> dict:
    """The board's policy block plus the review-round cap the board omits.

    ``board_policy`` is what ``private_view.shape_policy`` already resolved (it
    folds the legacy ``max_turns_per_task`` into ``max_task_turns_hard``);
    ``raw_policy`` is the stored control policy, read only for the cap.
    """
    shaped = {key: (board_policy or {}).get(key) for key in POLICY_KEYS}
    shaped["max_tasks"] = flatten_max_tasks(shaped.get("max_tasks"))
    rounds = (raw_policy or {}).get("max_review_rounds")
    shaped["max_review_rounds"] = (
        DEFAULT_MAX_REVIEW_ROUNDS if rounds is None else rounds
    )
    return shaped


def shape_pr(evidence: dict | None) -> dict | None:
    """The delivery's pull request, recovered from the finish_task evidence."""
    url = (evidence or {}).get("pr_url")
    if not url:
        return None
    tail = str(url).rstrip("/").rsplit("/", 1)[-1]
    return {
        "number": int(tail) if tail.isdigit() else None,
        "url": str(url),
        "state": (evidence or {}).get("state"),
    }


def shape_brief(body: str | None) -> list[str]:
    """The issue body as paragraphs, bounded so a long issue cannot dominate."""
    if not body:
        return []
    paragraphs = [
        block.strip()
        for block in re.split(r"\n[ \t]*\n", str(body).replace("\r\n", "\n"))
    ]
    return [block for block in paragraphs if block][:BRIEF_PARAGRAPHS]


def shape_stop_events(events: list[dict] | None) -> list[dict]:
    """Lifecycle stop events with the actor and the request key removed."""
    shaped = []
    for event in events or []:
        shaped.append(
            {
                **{key: event.get(key) for key in _STOP_EVENT_KEYS},
                "at": _iso(event.get("created_at")),
            }
        )
    return shaped


def latest_attempt_finish(nodes: list[dict] | None) -> str | None:
    """The last attempt to finish anywhere in the plan."""
    finishes = [
        attempt.get("finished_at")
        for node in nodes or []
        for attempt in node.get("attempts") or []
        if attempt.get("finished_at")
    ]
    return max(finishes) if finishes else None


def task_phase(receipt: dict) -> str:
    """The node the task is on: what is running, else what ran last."""
    nodes = receipt.get("nodes") or []
    running = next((node for node in nodes if node.get("state") == "running"), None)
    if running:
        return running["node_key"]
    started = [
        (
            max(
                attempt.get("created_at") or ""
                for attempt in node.get("attempts") or []
            ),
            node.get("node_key"),
        )
        for node in nodes
        if node.get("attempts")
    ]
    if started:
        return max(started)[1]
    return _PHASE_FALLBACK.get(receipt.get("state"), "queued")


def review_rounds(nodes: list[dict] | None) -> int:
    """How many engine-owned correction rounds this task has opened."""
    return sum(
        1
        for node in nodes or []
        if _CORRECTION_NODE.fullmatch(node.get("node_key") or "")
    )


def task_summary(receipt: dict, cost_usd: float | None = None) -> dict:
    """One board card: the task's identity, budget, and where it has got to.

    ``cost_usd`` is the task's spend at list price across every session it
    ran (planner, workers, reviewers). ``committed_cost_usd`` stays the budget
    ledger's figure, which mixes provider-reported costs with reserved
    ceilings and is what the task budget is enforced against.
    """
    nodes = receipt.get("nodes") or []
    state = receipt.get("state")
    allowance = receipt.get("allowance") or {}
    return {
        "issue_number": receipt.get("issue_number"),
        "generation": receipt.get("generation"),
        "title": receipt.get("title"),
        "url": receipt.get("url"),
        "state": RECEIPT_STATE_WORD.get(state, state),
        "phase": task_phase(receipt),
        "task_class": receipt.get("task_class"),
        "admitted_at": _iso(receipt.get("admitted_at")),
        "deadline_at": _iso(receipt.get("deadline_at")),
        "finished_at": (
            latest_attempt_finish(nodes) if state in TERMINAL_STATES else None
        ),
        "turns_used": receipt.get("turns_used"),
        "planner_turns_used": receipt.get("planner_turns_used"),
        "allowance_turns": allowance.get("turns"),
        "committed_cost_usd": _float(receipt.get("committed_cost_usd")),
        "cost_usd": cost_usd,
        "review_rounds": review_rounds(nodes),
        "pr": shape_pr(receipt.get("evidence")),
        "nodes": [
            {"node_key": node.get("node_key"), "state": node.get("state")}
            for node in nodes
        ],
    }


def shape_work_item(
    item: dict,
    edges_out: list[dict],
    edges_in: list[dict],
    snapshotted_at: str,
) -> dict:
    """A public GitHub-origin work item, without private execution records."""
    repo = item.get("github_repo")
    number = item.get("github_issue_number")
    source_ref = (
        f"https://github.com/{repo}/issues/{number}"
        if isinstance(repo, str) and type(number) is int and number > 0
        else None
    )

    def edge(row: dict, other: str) -> dict:
        return {
            other: row.get(other),
            "kind": row.get("kind"),
            "source": row.get("source"),
        }

    return {
        "snapshotted_at": snapshotted_at,
        "item": {
            "id": item.get("id"),
            "title": item.get("title"),
            "state": item.get("state"),
            "task_class": item.get("task_class"),
            "labels": list(item.get("labels") or []),
            "trust": item.get("trust"),
            "authority": item.get("authority"),
            "github_issue_number": number,
            "source_ref": source_ref,
            "created_at": _iso(item.get("created_at")),
            "updated_at": _iso(item.get("updated_at")),
        },
        "edges_out": [edge(row, "to_id") for row in edges_out],
        "edges_in": [edge(row, "from_id") for row in edges_in],
    }


def load_usage(usage_json: str | None) -> dict | None:
    """``usage_json`` is stored as a JSON string; a bad one is not a failure."""
    if not usage_json:
        return None
    try:
        parsed = json.loads(usage_json)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def shape_activities(usage: dict | None) -> list[dict]:
    """The tool calls the guest shim recorded, tail-bounded and key-filtered."""
    items = (usage or {}).get("activities")
    if not isinstance(items, list):
        return []
    shaped = []
    for item in items[-ACTIVITY_LIMIT:]:
        if not isinstance(item, dict):
            continue
        shaped.append({key: item[key] for key in _ACTIVITY_KEYS if key in item})
    return shaped


def shape_usage(usage: dict | None) -> dict | None:
    """Token counts only. Null when the turn recorded no usable numbers."""
    if not isinstance(usage, dict):
        return None
    cache_read = next(
        (usage[key] for key in _CACHE_READ_KEYS if usage.get(key) is not None), None
    )
    values = {
        "input_tokens": _int(usage.get("input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")),
        "cache_read_tokens": _int(cache_read),
    }
    return None if all(value is None for value in values.values()) else values


def decode_diff(blob: bytes | None, truncated: bool | None) -> tuple[str | None, bool]:
    """Decompress one turn's diff, capping the decoded text at DIFF_LIMIT.

    The stored flag already says the capture itself was cut short; cutting here
    sets it too, so a reader never mistakes a clipped diff for a whole one.
    """
    if not blob:
        return None, bool(truncated)
    # Bounded decompress, the transport.py idiom: ask for one byte past the cap
    # so a blob that decodes to more than DIFF_LIMIT is detected without ever
    # materialising the rest of it.
    try:
        raw = zlib.decompressobj().decompress(blob, DIFF_LIMIT + 1)
    except (zlib.error, TypeError):
        logger.warning("factory_public.diff_undecodable")
        return None, True
    if len(raw) > DIFF_LIMIT:
        return raw[:DIFF_LIMIT].decode("utf-8", "replace"), True
    return raw.decode("utf-8", "replace"), bool(truncated)


def shape_rationale(result_text: str | None) -> dict | None:
    """The rationale trailer, wrapped to the two fields the page shows.

    There is no rationale column: it is parsed out of the result at render
    time. ``paths`` and ``deviations`` stay private, so the public page gets
    the raw trailer and whether it parsed.
    """
    from factory.execution.rationale import parse_rationale

    parsed = parse_rationale(result_text)
    if parsed.get("parse_status") == "none":
        return None
    return {"raw": parsed.get("raw"), "parse_status": parsed.get("parse_status")}


def shape_permission_denials(value) -> list:
    """The denial list, stored as a JSON string. Anything else reads as none."""
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
)


def _has_tokens(usage: dict | None) -> bool:
    return any((_int((usage or {}).get(key)) or 0) > 0 for key in _TOKEN_KEYS)


def turn_list_cost(turn: dict, usage: dict | None = None) -> float | None:
    """A turn's cost at list price, the only basis the public pages show.

    The turn store prices every turn (shared/pricing.py). A turn that never
    reached a model records no tokens and is left unpriced there, but it cost
    nothing, so it reads 0.0 here rather than unknown. None means tokens were
    spent on a model the price table does not know.
    """
    listed = _float(turn.get("list_cost_usd"))
    if listed is not None:
        return listed
    if usage is None:
        usage = load_usage(turn.get("usage_json"))
    if usage is None:
        return None
    return None if _has_tokens(usage) else 0.0


def sum_list_costs(turns: list[dict]) -> float | None:
    """Total list cost of some turns, or None when none of them is known."""
    known = [
        cost for cost in (turn_list_cost(turn) for turn in turns) if cost is not None
    ]
    return float(sum(known)) if known else None


def turn_digest(turn: dict) -> dict:
    """What a turn looks like inside a task walkthrough: no diff, no rationale."""
    usage = load_usage(turn.get("usage_json"))
    return {
        "seq": turn.get("seq"),
        "prompt": turn.get("prompt"),
        "activities": shape_activities(usage),
        "result_text": turn.get("result_text"),
        "cost_usd": turn_list_cost(turn, usage),
        "commit_sha": turn.get("commit_sha"),
    }


def turn_record(turn: dict) -> dict:
    """One turn in full: the digest plus the diff, rationale and usage."""
    usage = load_usage(turn.get("usage_json"))
    diff, truncated = decode_diff(turn.get("diff_blob"), turn.get("diff_truncated"))
    return {
        **turn_digest(turn),
        "base_sha": turn.get("base_sha") or turn.get("diff_base_sha"),
        "diff": diff,
        "diff_truncated": truncated,
        "rationale": shape_rationale(turn.get("result_text")),
        "usage": shape_usage(usage),
        "stop_reason": turn.get("stop_reason"),
        "terminal_reason": turn.get("terminal_reason"),
        "permission_denials": shape_permission_denials(turn.get("permission_denials")),
        "created_at": _iso(turn.get("created_at")),
    }


def shape_node(
    node: dict, session_keys: dict[int, str], turns: dict[int, list]
) -> dict:
    """One plan node with its attempts, each attempt carrying its turn digests."""
    attempts = []
    for attempt in node.get("attempts") or []:
        session_id = attempt.get("session_id")
        attempts.append(
            {
                "attempt": attempt.get("attempt"),
                "status": attempt.get("status"),
                # List price of the attempt's own turns, not the settlement
                # figure the budget books (provider-reported or reserved).
                "cost_usd": (
                    sum_list_costs(turns[session_id]) if session_id in turns else None
                ),
                "session_key": session_keys.get(session_id),
                "created_at": _iso(attempt.get("created_at")),
                "finished_at": _iso(attempt.get("finished_at")),
                "turns": [turn_digest(turn) for turn in turns.get(session_id) or []],
            }
        )
    return {
        "node_key": node.get("node_key"),
        "label": node.get("label"),
        "kind": node.get("kind"),
        "model": node.get("model"),
        "deps": list(node.get("deps") or []),
        "state": node.get("state"),
        "attempts": attempts,
    }


def task_payload(
    receipt: dict,
    policy: dict,
    body: str | None,
    session_keys: dict[int, str],
    turns: dict[int, list],
    snapshotted_at: str,
    cost_usd: float | None = None,
) -> dict:
    """One task's walkthrough page."""
    return {
        "snapshotted_at": snapshotted_at,
        "policy": policy,
        "task": {
            **task_summary(receipt, cost_usd),
            "brief": shape_brief(body),
            "evidence_reason": (receipt.get("evidence") or {}).get("reason"),
            "nodes": [
                shape_node(node, session_keys, turns)
                for node in receipt.get("nodes") or []
            ],
            "stop_events": shape_stop_events(receipt.get("stop_events")),
        },
    }


def session_payload(
    row: dict,
    issue_number: int | None,
    node_key: str | None,
    attempt: int | None,
    turns: list[dict],
    snapshotted_at: str,
) -> dict:
    """One attempt's full record."""
    last = turns[-1] if turns else {}
    return {
        "snapshotted_at": snapshotted_at,
        "session": {
            "key": row.get("local_session_id"),
            "issue_number": issue_number,
            "node_key": node_key,
            "attempt": attempt,
            "model": row.get("model"),
            "status": row.get("status"),
            "guest_bound": bool(row.get("ember_session_id")),
            "created_at": _iso(row.get("created_at")),
            "last_turn_at": _iso(row.get("last_turn_at")),
            "terminal_reason": last.get("terminal_reason"),
            "turn_count": len(turns),
            "cost_usd": sum_list_costs(turns),
        },
        "turns": [turn_record(turn) for turn in turns],
    }


@dataclass(frozen=True)
class PublicSnapshot:
    """The public payload kinds, keyed the way the snapshot tables key them."""

    activity: dict
    tasks: dict[int, dict]
    sessions: dict[str, dict]
    session_issues: dict[str, int | None]
    work_items: dict[int, dict]


def _public_work_items(db: Session, snapshotted_at: str) -> dict[int, dict]:
    """Shape only GitHub-origin rows and edges whose endpoints are both public."""
    from factory.orchestration.factory_models import WorkItem, WorkItemEdge

    rows = db.exec(
        select(WorkItem)
        .where(
            WorkItem.source_kind == "github",
            WorkItem.github_repo.is_not(None),
            WorkItem.github_issue_number.is_not(None),
        )
        .order_by(WorkItem.id)
    ).all()
    ids = {row.id for row in rows if row.id is not None}
    edges = (
        db.exec(
            select(WorkItemEdge)
            .where(
                WorkItemEdge.from_id.in_(ids),
                WorkItemEdge.to_id.in_(ids),
            )
            .order_by(WorkItemEdge.id)
        ).all()
        if ids
        else []
    )
    result = {}
    for row in rows:
        if row.id is None:
            continue
        item = {
            key: getattr(row, key)
            for key in (
                "id",
                "title",
                "state",
                "task_class",
                "labels",
                "trust",
                "authority",
                "github_repo",
                "github_issue_number",
                "created_at",
                "updated_at",
            )
        }
        result[row.id] = shape_work_item(
            item,
            [edge.model_dump() for edge in edges if edge.from_id == row.id],
            [edge.model_dump() for edge in edges if edge.to_id == row.id],
            snapshotted_at,
        )
    return result


def _board_task_ids(db: Session) -> tuple[set[str], dict[int, str]]:
    """The task ids the board will show, and every receipt's issue body.

    ``build_factory_view`` decides in flight / queued / recent itself, so this
    mirrors its split rather than inventing one: passing a task id that turns
    out not to be on the board only costs a plan read that nothing renders.
    """
    from factory.orchestration.factory_models import FactoryReceipt
    from factory.private_view import ACTIVE_STATES, QUEUED_STATES, RECENT_LIMIT

    rows = db.exec(
        select(
            FactoryReceipt.id,
            FactoryReceipt.state,
            FactoryReceipt.task_id,
            FactoryReceipt.body,
        ).order_by(FactoryReceipt.id)
    ).all()
    live = [row for row in rows if row.state in ACTIVE_STATES]
    recent = sorted(
        (row for row in rows if row.state not in ACTIVE_STATES + QUEUED_STATES),
        key=lambda row: row.id,
        reverse=True,
    )[:RECENT_LIMIT]
    task_ids = {row.task_id for row in (*live, *recent) if row.task_id}
    return task_ids, {row.id: row.body for row in rows}


def _raw_policy(db: Session) -> dict:
    """The stored control policy, read for the fields the board view drops."""
    from factory.orchestration.factory_models import FactoryControl

    control = db.exec(
        select(FactoryControl).where(FactoryControl.id == "factory")
    ).first()
    if control is None or not control.policy_json:
        return {}
    try:
        parsed = json.loads(control.policy_json)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


_TURN_COLUMNS = (
    "seq",
    "prompt",
    "result_text",
    "terminal_reason",
    "stop_reason",
    "permission_denials",
    "commit_sha",
    "base_sha",
    "diff_blob",
    "diff_truncated",
    "diff_base_sha",
    "usage_json",
    "cost_usd",
    "list_cost_usd",
    "created_at",
)
_SESSION_COLUMNS = (
    "id",
    "local_session_id",
    "model",
    "status",
    "ember_session_id",
    "created_at",
    "last_turn_at",
)


def _sessions_and_turns(
    db: Session, ids: set[int]
) -> tuple[dict[int, dict], dict[int, list[dict]]]:
    """Every referenced session row and its turns, in two reads rather than 2N."""
    from factory.execution.models import AgentSession, AgentTurn

    if not ids:
        return {}, {}
    sessions = {
        row.id: {column: getattr(row, column) for column in _SESSION_COLUMNS}
        for row in db.exec(select(AgentSession).where(AgentSession.id.in_(ids))).all()
    }
    turns: dict[int, list[dict]] = {}
    for row in db.exec(
        select(AgentTurn)
        .where(AgentTurn.session_id.in_(ids))
        .order_by(AgentTurn.session_id, AgentTurn.seq)
    ).all():
        turns.setdefault(row.session_id, []).append(
            {column: getattr(row, column) for column in _TURN_COLUMNS}
        )
    return sessions, turns


# Factory sessions are keyed factory:<task_id>:<node_key>:<attempt>, planner
# and conductor rounds included, so the task id prefix finds every session a
# task ran without walking its plan. NULL list costs are turns that recorded
# no tokens (see turn_list_cost); they cost nothing.
_TASK_LIST_COST = text(
    """
    SELECT split_part(s.local_session_id, ':', 2) AS task_id,
           COALESCE(SUM(t.list_cost_usd), 0) AS list_cost_usd
    FROM agent_sessions.agent_sessions s
    JOIN agent_sessions.agent_turns t ON t.session_id = s.id
    WHERE s.local_session_id LIKE 'factory:%'
      AND split_part(s.local_session_id, ':', 2) IN :task_ids
    GROUP BY 1
    """
).bindparams(bindparam("task_ids", expanding=True, type_=String()))

LEDGER_WINDOW_DAYS = 7
# The board's 7-day strip, counted over every receipt rather than the board's
# capped recent list. A task finishes when its last node run does; a receipt
# that settled without running one (cancelled from the queue) falls back to
# its last update.
_LEDGER_OUTCOMES = text(
    """
    WITH finished AS (
        SELECT r.state,
               COALESCE(
                   (SELECT MAX(n.finished_at)
                    FROM swarm.swarm_node_run n
                    WHERE n.task_id = r.task_id),
                   r.updated_at
               ) AS finished_at
        FROM swarm.factory_receipt r
        WHERE r.state IN ('succeeded', 'failed', 'escalated')
    )
    SELECT
        COUNT(*) FILTER (WHERE state = 'succeeded') AS landed,
        COUNT(*) FILTER (WHERE state IN ('failed', 'escalated')) AS escalated
    FROM finished
    WHERE finished_at >= :since
    """
)
_LEDGER_SPEND = text(
    """
    SELECT COALESCE(SUM(t.list_cost_usd), 0) AS spend_usd,
           COUNT(*) AS turns
    FROM agent_sessions.agent_turns t
    JOIN agent_sessions.agent_sessions s ON s.id = t.session_id
    WHERE t.created_at >= :since
      AND s.local_session_id LIKE 'factory:%'
    """
)


def _task_list_costs(db: Session, task_ids: set[str]) -> dict[str, float]:
    """List-price spend per task across every session it ran."""
    if not task_ids:
        return {}
    rows = db.execute(_TASK_LIST_COST, {"task_ids": sorted(task_ids)}).all()
    return {row.task_id: float(row.list_cost_usd) for row in rows}


def ledger_totals(db: Session, now: datetime) -> dict:
    """Landed, escalated and list-price spend over the last seven days."""
    since = now - timedelta(days=LEDGER_WINDOW_DAYS)
    outcomes = db.execute(_LEDGER_OUTCOMES, {"since": since}).one()
    spend = db.execute(_LEDGER_SPEND, {"since": since}).one()
    return {
        "window_days": LEDGER_WINDOW_DAYS,
        "cost_basis": "list",
        "landed": int(outcomes.landed or 0),
        "escalated": int(outcomes.escalated or 0),
        "spend_usd": float(spend.spend_usd or 0),
    }


def build_public_snapshot(
    session: Session, *, provider_quota: dict | None = None
) -> PublicSnapshot:
    """Read the factory once and shape everything the public pages serve."""
    from factory.private_view import build_factory_view

    now = datetime.now(timezone.utc)
    snapshotted_at = _iso(now)
    quota = shape_quota(provider_quota, now)
    work_items = _public_work_items(session, snapshotted_at)
    task_ids, bodies = _board_task_ids(session)
    board = build_factory_view(session=session, plan_tasks=task_ids)
    policy = shape_policy(board.get("policy"), _raw_policy(session))

    if not board.get("ok"):
        return PublicSnapshot(
            activity={
                "snapshotted_at": snapshotted_at,
                "state": board.get("state"),
                "policy": policy,
                "active": [],
                "queued": [],
                "recent": [],
                "totals_7d": ledger_totals(session, now),
                "quota": quota,
            },
            tasks={},
            sessions={},
            session_issues={},
            work_items=work_items,
        )

    cards = [
        *(board.get("active") or []),
        *(board.get("queued") or []),
        *(board.get("recent") or []),
    ]
    session_ids = {
        attempt["session_id"]
        for card in cards
        for node in card.get("nodes") or []
        for attempt in node.get("attempts") or []
        if attempt.get("session_id")
    }
    session_rows, turns = _sessions_and_turns(session, session_ids)
    session_keys = {sid: row["local_session_id"] for sid, row in session_rows.items()}
    task_costs = _task_list_costs(
        session, {card["task_id"] for card in cards if card.get("task_id")}
    )

    def card_cost(card: dict) -> float | None:
        task_id = card.get("task_id")
        if not task_id:
            return None
        return task_costs.get(task_id, 0.0)

    activity = {
        "snapshotted_at": snapshotted_at,
        "state": board.get("state"),
        "policy": policy,
        "active": [
            task_summary(card, card_cost(card)) for card in board.get("active") or []
        ],
        "queued": [
            task_summary(card, card_cost(card)) for card in board.get("queued") or []
        ],
        "recent": [
            task_summary(card, card_cost(card)) for card in board.get("recent") or []
        ],
        "totals_7d": ledger_totals(session, now),
        "quota": quota,
    }

    tasks: dict[int, dict] = {}
    sessions: dict[str, dict] = {}
    session_issues: dict[str, int | None] = {}
    for card in cards:
        issue_number = card.get("issue_number")
        if issue_number is None:
            continue
        tasks[issue_number] = task_payload(
            card,
            policy,
            bodies.get(card.get("id")),
            session_keys,
            turns,
            snapshotted_at,
            card_cost(card),
        )
        for node in card.get("nodes") or []:
            for attempt in node.get("attempts") or []:
                sid = attempt.get("session_id")
                row = session_rows.get(sid)
                if row is None:
                    continue
                key = row["local_session_id"]
                sessions[key] = session_payload(
                    row,
                    issue_number,
                    node.get("node_key"),
                    attempt.get("attempt"),
                    turns.get(sid) or [],
                    snapshotted_at,
                )
                session_issues[key] = issue_number

    return PublicSnapshot(
        activity=activity,
        tasks=tasks,
        sessions=sessions,
        session_issues=session_issues,
        work_items=work_items,
    )


_QUOTA_HISTORY_READ = text(
    "SELECT payload->'quota'->'history' "
    "FROM public_api.factory_activity_snapshot WHERE id = 1 FOR UPDATE"
)
_ACTIVITY_UPSERT = text(
    """
    INSERT INTO public_api.factory_activity_snapshot (id, payload, snapshotted_at)
    VALUES (1, CAST(:payload AS jsonb), :snapshotted_at)
    ON CONFLICT (id) DO UPDATE
    SET payload = EXCLUDED.payload, snapshotted_at = EXCLUDED.snapshotted_at
    """
)
_TASK_UPSERT = text(
    """
    INSERT INTO public_api.factory_task_snapshot
        (issue_number, payload, snapshotted_at)
    VALUES (:issue_number, CAST(:payload AS jsonb), :snapshotted_at)
    ON CONFLICT (issue_number) DO UPDATE
    SET payload = EXCLUDED.payload, snapshotted_at = EXCLUDED.snapshotted_at
    """
)
_SESSION_UPSERT = text(
    """
    INSERT INTO public_api.factory_session_snapshot
        (session_key, issue_number, payload, snapshotted_at)
    VALUES (:session_key, :issue_number, CAST(:payload AS jsonb), :snapshotted_at)
    ON CONFLICT (session_key) DO UPDATE
    SET issue_number = EXCLUDED.issue_number,
        payload = EXCLUDED.payload,
        snapshotted_at = EXCLUDED.snapshotted_at
    """
)
_WORK_ITEM_UPSERT = text(
    """
    INSERT INTO public_api.factory_work_item_snapshot
        (work_item_id, payload, snapshotted_at)
    VALUES (:work_item_id, CAST(:payload AS jsonb), :snapshotted_at)
    ON CONFLICT (work_item_id) DO UPDATE
    SET payload = EXCLUDED.payload, snapshotted_at = EXCLUDED.snapshotted_at
    """
)
# The board is a rolling window, so a task that falls off the recent list stops
# being published. Without these the tables would grow without bound.
# The expanding bindparams carry an explicit type: with an EMPTY keep-list
# SQLAlchemy renders the placeholder as CAST(NULL AS <type>), and an untyped
# one defaults to INTEGER, which Postgres refuses against the TEXT session_key.
_TASK_PRUNE = text(
    "DELETE FROM public_api.factory_task_snapshot WHERE issue_number NOT IN :keep"
).bindparams(bindparam("keep", expanding=True, type_=Integer()))
_SESSION_PRUNE = text(
    "DELETE FROM public_api.factory_session_snapshot WHERE session_key NOT IN :keep"
).bindparams(bindparam("keep", expanding=True, type_=String()))
_WORK_ITEM_PRUNE = text(
    "DELETE FROM public_api.factory_work_item_snapshot WHERE work_item_id NOT IN :keep"
).bindparams(bindparam("keep", expanding=True, type_=BigInteger()))


def _encode(payload: dict) -> str:
    """JSON for a jsonb parameter, with non-ASCII sent as UTF-8 bytes.

    ``ensure_ascii`` would emit ``\\u00b7`` escapes for the middle dots in node
    labels, and a server whose encoding is SQL_ASCII (the CI database) refuses
    those inside jsonb. Raw UTF-8 is accepted by both that server and the UTF-8
    production cluster.
    """
    sanitized = sanitize_payload(payload)
    return json.dumps(sanitized, ensure_ascii=False)


def write_public_snapshot(
    session: Session, *, provider_quota: dict | None = None
) -> dict:
    """Build the snapshot and replace what the public tables hold with it.

    Write the activity payload first (fail loudly if it fails, no partial board).
    Write each task and session inside its own savepoint to isolate failures:
    one bad row no longer takes down the whole snapshot.
    """
    # Serialize read/replace with the existing singleton. Its history survives
    # ordinary board refreshes and failed broker fetches without a new table.
    previous_history = session.execute(_QUOTA_HISTORY_READ).scalar_one_or_none()
    snapshot = build_public_snapshot(session, provider_quota=provider_quota)
    at = snapshot.activity["snapshotted_at"]
    quota = snapshot.activity["quota"]
    quota["history"] = shape_quota_history(quota, previous_history)

    # Write activity first; if this fails, the job fails loudly.
    session.execute(
        _ACTIVITY_UPSERT,
        {"payload": _encode(snapshot.activity), "snapshotted_at": at},
    )

    tasks_skipped = 0
    for issue_number, payload in snapshot.tasks.items():
        savepoint = session.begin_nested()
        try:
            session.execute(
                _TASK_UPSERT,
                {
                    "issue_number": issue_number,
                    "payload": _encode(payload),
                    "snapshotted_at": at,
                },
            )
            savepoint.commit()
        except SQLAlchemyError as exc:
            savepoint.rollback()
            logger.warning(
                "factory_public.task_upsert_failed: issue %s skipped (%s)",
                issue_number,
                type(exc).__name__,
            )
            tasks_skipped += 1

    sessions_skipped = 0
    for key, payload in snapshot.sessions.items():
        savepoint = session.begin_nested()
        try:
            session.execute(
                _SESSION_UPSERT,
                {
                    "session_key": key,
                    "issue_number": snapshot.session_issues.get(key),
                    "payload": _encode(payload),
                    "snapshotted_at": at,
                },
            )
            savepoint.commit()
        except SQLAlchemyError as exc:
            savepoint.rollback()
            logger.warning(
                "factory_public.session_upsert_failed: session %s skipped (%s)",
                key,
                type(exc).__name__,
            )
            sessions_skipped += 1

    work_items_skipped = 0
    for work_item_id, payload in snapshot.work_items.items():
        savepoint = session.begin_nested()
        try:
            session.execute(
                _WORK_ITEM_UPSERT,
                {
                    "work_item_id": work_item_id,
                    "payload": _encode(payload),
                    "snapshotted_at": at,
                },
            )
            savepoint.commit()
        except SQLAlchemyError as exc:
            savepoint.rollback()
            logger.warning(
                "factory_public.work_item_upsert_failed: work item %s skipped (%s)",
                work_item_id,
                type(exc).__name__,
            )
            work_items_skipped += 1

    session.execute(_TASK_PRUNE, {"keep": list(snapshot.tasks)})
    session.execute(_SESSION_PRUNE, {"keep": list(snapshot.sessions)})
    session.execute(_WORK_ITEM_PRUNE, {"keep": list(snapshot.work_items)})
    session.commit()

    return {
        "tasks": len(snapshot.tasks),
        "sessions": len(snapshot.sessions),
        "work_items": len(snapshot.work_items),
        "tasks_skipped": tasks_skipped,
        "sessions_skipped": sessions_skipped,
        "work_items_skipped": work_items_skipped,
        "snapshotted_at": at,
    }


# --- Agent activity: the /slop/factory overview's aggregate payload ---------
#
# public_view.get_public_agent_activity used to aggregate the public_api
# agent_activity views on every request, which parses every turn's usage_json
# (each carries the turn's tool-call list) and took 4 to 6 seconds. This builds
# the same aggregates once per cadence, all at list price, and the public
# route reads the single row. Ember turns and local Mac sessions are summed on
# one basis so the overview's spend covers both.

ACTIVITY_DAILY_WINDOW_DAYS = 30
ACTIVITY_TOTAL_WINDOWS = {"totals_7d": 7, "totals_30d": 30}

_NUMERIC = r"^[0-9]+([.][0-9]+)?$"

# usage_json is parsed once per turn in the MATERIALIZED CTE; the NUL guard
# keeps a \u0000 escape (which jsonb refuses) from failing the whole snapshot.
# A turn with a NULL list cost and no tokens never reached a model and counts
# as 0.0; one with tokens and no price is counted as unpriced.
_ACTIVITY_DAILY = text(
    rf"""
    WITH parsed AS MATERIALIZED (
        SELECT t.created_at, t.session_id, t.list_cost_usd,
               CASE
                   WHEN t.usage_json LIKE '{{%'
                    AND t.usage_json !~ '(^|[^\\])(\\\\)*\\u0000'
                   THEN t.usage_json::jsonb
               END AS u
        FROM agent_sessions.agent_turns t
        WHERE t.created_at >= :since
    ),
    turns AS (
        SELECT (p.created_at AT TIME ZONE 'UTC')::date AS day,
               p.session_id,
               p.list_cost_usd,
               CASE WHEN p.u->>'input_tokens' ~ '{_NUMERIC}'
                    THEN (p.u->>'input_tokens')::numeric END AS input_tokens,
               CASE WHEN p.u->>'output_tokens' ~ '{_NUMERIC}'
                    THEN (p.u->>'output_tokens')::numeric END AS output_tokens,
               CASE WHEN COALESCE(p.u->>'cache_read_tokens',
                                  p.u->>'cache_read_input_tokens',
                                  p.u->>'cached_input_tokens') ~ '{_NUMERIC}'
                    THEN COALESCE(p.u->>'cache_read_tokens',
                                  p.u->>'cache_read_input_tokens',
                                  p.u->>'cached_input_tokens')::numeric
               END AS cache_read_tokens,
               CASE WHEN COALESCE(p.u->>'cache_write_tokens',
                                  p.u->>'cache_creation_input_tokens',
                                  p.u->>'cache_write_input_tokens') ~ '{_NUMERIC}'
                    THEN COALESCE(p.u->>'cache_write_tokens',
                                  p.u->>'cache_creation_input_tokens',
                                  p.u->>'cache_write_input_tokens')::numeric
               END AS cache_write_tokens
        FROM parsed p
    )
    SELECT turns.day,
           COALESCE(s.model, 'unknown') AS model,
           COUNT(DISTINCT turns.session_id) AS sessions,
           COUNT(*) AS turns,
           COALESCE(SUM(turns.input_tokens), 0) AS input_tokens,
           COALESCE(SUM(turns.output_tokens), 0) AS output_tokens,
           COALESCE(SUM(turns.cache_read_tokens), 0) AS cache_read_tokens,
           SUM(
               CASE
                   WHEN turns.list_cost_usd IS NOT NULL THEN turns.list_cost_usd
                   WHEN COALESCE(turns.input_tokens, 0)
                      + COALESCE(turns.output_tokens, 0)
                      + COALESCE(turns.cache_read_tokens, 0)
                      + COALESCE(turns.cache_write_tokens, 0) = 0 THEN 0
               END
           ) AS list_cost_usd,
           COUNT(*) FILTER (
               WHERE turns.list_cost_usd IS NULL
                 AND COALESCE(turns.input_tokens, 0)
                   + COALESCE(turns.output_tokens, 0)
                   + COALESCE(turns.cache_read_tokens, 0)
                   + COALESCE(turns.cache_write_tokens, 0) > 0
           ) AS unpriced_turns
    FROM turns
    JOIN agent_sessions.agent_sessions s ON s.id = turns.session_id
    GROUP BY 1, 2
    """
)
# Local Mac sessions (the claude-session and codex-session collectors). The
# collector, or price-raws-backfill, stores each session's list price as
# extra.usage_cost_usd. The collector re-uploads a session as it grows, and
# each upload is a cumulative snapshot of the same run (same session_id and
# started_at, later ended_at), sometimes byte-identical. Only the latest
# snapshot of each run counts, on the day it was uploaded; summing every
# snapshot counted one $45 session nine times. Codex forks share a session_id
# but start at different times, so they stay separate runs.
_LOCAL_ACTIVITY_DAILY = text(
    rf"""
    WITH latest AS (
        SELECT DISTINCT ON (
                   source,
                   COALESCE(extra->>'session_id', raw_id),
                   COALESCE(extra->>'started_at', '')
               )
               created_at, source, extra
        FROM knowledge.raw_inputs
        WHERE source IN ('claude-session', 'codex-session')
          AND extra ? 'usage'
          AND jsonb_typeof(extra->'usage') = 'object'
          AND created_at >= :since
        ORDER BY source,
                 COALESCE(extra->>'session_id', raw_id),
                 COALESCE(extra->>'started_at', ''),
                 id DESC
    ),
    raws AS (
        SELECT (created_at AT TIME ZONE 'UTC')::date AS day,
               COALESCE(extra->>'model', 'unknown') AS model,
               source,
               NULLIF(regexp_replace(extra->'usage'->>'input_tokens', '[^0-9]', '', 'g'), '')::numeric AS input_tokens,
               NULLIF(regexp_replace(extra->'usage'->>'output_tokens', '[^0-9]', '', 'g'), '')::numeric AS output_tokens,
               NULLIF(regexp_replace(extra->'usage'->>'cache_read_tokens', '[^0-9]', '', 'g'), '')::numeric AS cache_read_tokens,
               CASE WHEN extra->>'usage_cost_usd' ~ '{_NUMERIC}'
                    THEN (extra->>'usage_cost_usd')::numeric END AS list_cost_usd
        FROM latest
    )
    SELECT day, model, source,
           COUNT(*) AS sessions,
           COALESCE(SUM(input_tokens), 0) AS input_tokens,
           COALESCE(SUM(output_tokens), 0) AS output_tokens,
           COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
           SUM(
               CASE
                   WHEN list_cost_usd IS NOT NULL THEN list_cost_usd
                   WHEN COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)
                      + COALESCE(cache_read_tokens, 0) = 0 THEN 0
               END
           ) AS list_cost_usd,
           COUNT(*) FILTER (
               WHERE list_cost_usd IS NULL
                 AND COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)
                   + COALESCE(cache_read_tokens, 0) > 0
           ) AS unpriced_sessions
    FROM raws
    GROUP BY 1, 2, 3
    """
)
_ACTIVITY_NOW = text(
    """
    SELECT
      (SELECT COUNT(*) FROM agent_sessions.agent_sessions
        WHERE last_turn_at > now() - interval '1 hour') AS active_last_hour,
      (SELECT COUNT(*) FROM agent_sessions.agent_sessions
        WHERE created_at >= date_trunc('day', now())) AS sessions_today,
      (SELECT MAX(last_turn_at) FROM agent_sessions.agent_sessions) AS last_turn_at,
      (SELECT COUNT(*) FROM agent_sessions.agent_sessions
        WHERE status = 'running') AS running
    """
)
_AGENT_ACTIVITY_UPSERT = text(
    """
    INSERT INTO public_api.agent_activity_snapshot (id, payload, snapshotted_at)
    VALUES (1, CAST(:payload AS jsonb), :snapshotted_at)
    ON CONFLICT (id) DO UPDATE
    SET payload = EXCLUDED.payload, snapshotted_at = EXCLUDED.snapshotted_at
    """
)


def _day_iso(value) -> str:
    if isinstance(value, datetime):
        value = value.date()
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _activity_totals(rows: list[dict]) -> dict:
    fields = ("sessions", "turns", "input_tokens", "output_tokens", "cache_read_tokens")
    result = {field: sum(row[field] for row in rows) for field in fields}
    known = [row["list_cost_usd"] for row in rows if row["list_cost_usd"] is not None]
    result["list_cost_usd"] = float(sum(known)) if known else None
    result["unpriced"] = sum(row["unpriced"] for row in rows)
    return result


def shape_agent_activity(
    now_row,
    daily_rows: list,
    local_daily_rows: list,
    *,
    today,
    snapshotted_at: str | None = None,
) -> dict:
    """Shape the overview's activity payload, every cost at list price.

    ``spend_daily`` carries every day of the 30-day window, ember and local
    together, so a quiet day reads $0 rather than a gap. A day is null only
    when everything it spent was on models the price table cannot price.
    """
    daily_start = today - timedelta(days=ACTIVITY_DAILY_WINDOW_DAYS - 1)

    def in_window(row) -> str | None:
        day = row_value(row, "day")
        if isinstance(day, datetime):
            day = day.date()
        if day < daily_start or day > today:
            return None
        return _day_iso(day)

    daily = []
    for row in daily_rows:
        day = in_window(row)
        if day is None:
            continue
        daily.append(
            {
                "day": day,
                "model": row_value(row, "model"),
                "sessions": int(row_value(row, "sessions") or 0),
                "turns": int(row_value(row, "turns") or 0),
                "input_tokens": int(row_value(row, "input_tokens") or 0),
                "output_tokens": int(row_value(row, "output_tokens") or 0),
                "cache_read_tokens": int(row_value(row, "cache_read_tokens") or 0),
                "list_cost_usd": _float(row_value(row, "list_cost_usd")),
                "unpriced": int(row_value(row, "unpriced_turns") or 0),
            }
        )
    daily.sort(key=lambda row: row["model"])
    daily.sort(key=lambda row: row["day"], reverse=True)

    local_daily = []
    for row in local_daily_rows:
        day = in_window(row)
        if day is None:
            continue
        local_daily.append(
            {
                "day": day,
                "model": row_value(row, "model"),
                "source": row_value(row, "source"),
                "sessions": int(row_value(row, "sessions") or 0),
                "turns": 0,
                "input_tokens": int(row_value(row, "input_tokens") or 0),
                "output_tokens": int(row_value(row, "output_tokens") or 0),
                "cache_read_tokens": int(row_value(row, "cache_read_tokens") or 0),
                "list_cost_usd": _float(row_value(row, "list_cost_usd")),
                "unpriced": int(row_value(row, "unpriced_sessions") or 0),
            }
        )
    local_daily.sort(key=lambda row: (row["model"], row["source"]))
    local_daily.sort(key=lambda row: row["day"], reverse=True)

    payload = {
        "snapshotted_at": snapshotted_at,
        "cost_basis": "list",
        "now": {
            "active_last_hour": int(row_value(now_row, "active_last_hour") or 0),
            "sessions_today": int(row_value(now_row, "sessions_today") or 0),
            "running": int(row_value(now_row, "running") or 0),
            "last_turn_at": _iso(row_value(now_row, "last_turn_at")),
        },
        "daily": daily,
        "local_daily": local_daily,
    }

    spend_daily = []
    for offset in range(ACTIVITY_DAILY_WINDOW_DAYS - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        ember = _activity_totals([row for row in daily if row["day"] == day])
        local = _activity_totals([row for row in local_daily if row["day"] == day])
        known = [
            value
            for value in (ember["list_cost_usd"], local["list_cost_usd"])
            if value is not None
        ]
        has_rows = any(row["day"] == day for row in (*daily, *local_daily))
        spend_daily.append(
            {
                "day": day,
                "spend_usd": (
                    float(sum(known)) if known else (None if has_rows else 0.0)
                ),
                "ember_usd": ember["list_cost_usd"],
                "local_usd": local["list_cost_usd"],
            }
        )
    payload["spend_daily"] = spend_daily

    for key, days in ACTIVITY_TOTAL_WINDOWS.items():
        start = (today - timedelta(days=days - 1)).isoformat()
        ember = _activity_totals([row for row in daily if row["day"] >= start])
        local = _activity_totals([row for row in local_daily if row["day"] >= start])
        combined = _activity_totals(
            [row for row in (*daily, *local_daily) if row["day"] >= start]
        )
        # spend_usd is what the overview tile reads; it is the same list-price
        # total as combined.list_cost_usd, kept under its old name.
        combined["spend_usd"] = combined["list_cost_usd"]
        payload[key] = {"ember": ember, "local": local, "combined": combined}
    return payload


def build_agent_activity(session: Session, now: datetime | None = None) -> dict:
    """Run the three aggregate reads and shape the overview payload."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    since = datetime.combine(
        today - timedelta(days=ACTIVITY_DAILY_WINDOW_DAYS - 1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    now_row = session.execute(_ACTIVITY_NOW).one()
    daily_rows = list(session.execute(_ACTIVITY_DAILY, {"since": since}).all())
    local_rows = list(session.execute(_LOCAL_ACTIVITY_DAILY, {"since": since}).all())
    return shape_agent_activity(
        now_row, daily_rows, local_rows, today=today, snapshotted_at=_iso(now)
    )


def write_agent_activity_snapshot(session: Session) -> dict:
    """Build the overview payload and upsert it into its one public row."""
    payload = build_agent_activity(session)
    session.execute(
        _AGENT_ACTIVITY_UPSERT,
        {"payload": _encode(payload), "snapshotted_at": payload["snapshotted_at"]},
    )
    session.commit()
    totals = payload["totals_7d"]["combined"]
    return {
        "snapshotted_at": payload["snapshotted_at"],
        "spend_7d_usd": totals["spend_usd"],
        "unpriced_7d": totals["unpriced"],
        "spend_30d_usd": payload["totals_30d"]["combined"]["spend_usd"],
    }
