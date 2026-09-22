"""Cached token broker quota observations and advisory health."""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

BROKER_URL_ENV = "EMBER_TOKENBROKER_URL"
PROVIDER_QUOTA_TTL_SECONDS = 30

_cache: tuple[float, dict] | None = None


def _broker_url() -> str:
    url = os.environ.get(BROKER_URL_ENV, "")
    if not url:
        raise ValueError("token broker is not configured")
    return url.rstrip("/")


def _cached_or(now: float, force: bool) -> dict | None:
    if not force and _cache is not None:
        cached_at, result = _cache
        if now - cached_at < PROVIDER_QUOTA_TTL_SECONDS:
            return result
    return None


def _store_result(now: float, result: dict) -> dict:
    global _cache
    _cache = (now, result)
    return result


def _available_result(payload: object) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), dict):
        raise ValueError("quota response has no providers object")
    grants = payload.get("grants")
    return {
        "available": True,
        "providers": payload["providers"],
        # Per-grant views (one account each). New brokers enumerate configured
        # but unobserved quota grants too, and explicitly mark that inventory
        # complete so admission never trusts an older partial response.
        "grants": grants if isinstance(grants, dict) else {},
        "grants_complete": payload.get("grants_complete") is True,
        "grants_valid": isinstance(grants, dict),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _unavailable_result(reason: str, exc: Exception | None = None) -> dict:
    logger.debug("provider quota unavailable: %s", reason, exc_info=exc is not None)
    return {"available": False, "reason": reason, "providers": {}}


def _classify(result_or_exc: Exception | httpx.Response) -> dict:
    """Convert a broker response or request failure into a result dictionary."""
    if isinstance(result_or_exc, Exception):
        return _request_failure(result_or_exc)

    try:
        result_or_exc.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _request_failure(exc)

    try:
        return _available_result(result_or_exc.json())
    except (TypeError, ValueError) as exc:
        return _unavailable_result("invalid broker response", exc)


def _request_failure(exc: Exception) -> dict:
    if isinstance(exc, httpx.TimeoutException):
        reason = "broker request timed out"
    elif isinstance(exc, httpx.HTTPStatusError):
        reason = f"broker returned {exc.response.status_code}"
    elif isinstance(exc, httpx.RequestError):
        reason = "broker unavailable"
    else:
        reason = "broker request failed"
    return _unavailable_result(reason, exc)


async def fetch_provider_quota(*, force: bool = False) -> dict:
    """Fetch provider quotas without allowing broker failure to escape."""
    now = time.monotonic()
    cached = _cached_or(now, force)
    if cached is not None:
        return cached

    try:
        url = _broker_url() + "/quota"
    except ValueError as exc:
        return _store_result(now, _unavailable_result(str(exc), exc))

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url)
        result = _classify(response)
    # nosemgrep: no-broad-except-swallow
    except Exception as exc:  # noqa: BLE001
        result = _classify(exc)
    return _store_result(now, result)


def fetch_provider_quota_sync(*, force: bool = False) -> dict:
    """Synchronously fetch provider quotas using the shared TTL cache."""
    now = time.monotonic()
    cached = _cached_or(now, force)
    if cached is not None:
        return cached

    try:
        url = _broker_url() + "/quota"
    except ValueError as exc:
        return _store_result(now, _unavailable_result(str(exc), exc))

    try:
        with httpx.Client(timeout=5) as client:
            response = client.get(url)
        result = _classify(response)
    # nosemgrep: no-broad-except-swallow
    except Exception as exc:  # noqa: BLE001
        result = _classify(exc)
    return _store_result(now, result)


def reset_cache() -> None:
    """Clear the process-local quota cache for tests."""
    global _cache
    _cache = None


def _headline_window(provider: str, windows: object) -> dict | None:
    if not isinstance(windows, list):
        return None
    active = [
        window
        for window in windows
        if isinstance(window, dict) and not window.get("expired", False)
    ]
    preferred = "primary" if provider == "codex" else "5h"
    return next(
        (window for window in active if window.get("name") == preferred),
        active[0] if active else None,
    )


def _active_windows(windows: object) -> list[dict]:
    """Retain every non-expired window and mark unusable evidence explicitly."""
    if not isinstance(windows, list):
        return []
    active = []
    for window in windows:
        if not isinstance(window, dict):
            active.append(
                {
                    "name": None,
                    "used_percent": None,
                    "resets_at": None,
                    "usable": False,
                }
            )
            continue
        if window.get("expired") is True:
            continue
        name = window.get("name")
        used = window.get("used_percent")
        usable = (
            isinstance(name, str)
            and bool(name)
            and isinstance(used, (int, float))
            and not isinstance(used, bool)
            and math.isfinite(float(used))
            and 0.0 <= float(used) <= 100.0
            and window.get("expired") in (None, False)
        )
        active.append(
            {
                "name": name if isinstance(name, str) and name else None,
                "used_percent": float(used)
                if isinstance(used, (int, float))
                and not isinstance(used, bool)
                and math.isfinite(float(used))
                else None,
                "resets_at": window.get("resets_at"),
                "usable": usable,
            }
        )
    return active


def _preferred_window_name(provider: str) -> str:
    return "primary" if provider == "codex" else "5h"


def summarise_grants(grants: object) -> tuple[dict, dict[str, bool]]:
    """Summarise grants and retain provider-scoped inventory validity."""
    providers = ("codex", "claude")
    validity = {provider: isinstance(grants, dict) for provider in providers}
    if not isinstance(grants, dict):
        return {}, validity
    summary = {}
    for name, value in grants.items():
        if not isinstance(value, dict):
            # Without a provider this entry could describe either quota class.
            # Keep the valid siblings, but neither class may trust completeness.
            validity = {provider: False for provider in providers}
            continue
        provider = value.get("provider")
        if not isinstance(provider, str) or not provider:
            validity = {known: False for known in providers}
            continue
        if provider not in providers:
            # Non-quota service-account grants are unrelated to this contract.
            continue
        if not isinstance(name, str) or not name:
            validity[provider] = False
            continue
        observed = value.get("observed")
        if observed is True:
            summary[name] = {
                **_summarise_view(provider, value),
                "grant": name,
                "provider": provider,
            }
        elif observed is False:
            summary[name] = {
                "grant": name,
                "provider": provider,
                "observed": False,
            }
        else:
            validity[provider] = False
            summary[name] = {
                "grant": name,
                "provider": provider,
                "observed": False,
                "usable": False,
            }
    return summary, validity


def summarise(providers: dict) -> dict:
    """Summarise each observed provider without discarding active windows."""
    summary = {}
    for provider in ("codex", "claude"):
        value = providers.get(provider)
        if not isinstance(value, dict) or value.get("observed") is not True:
            continue
        summary[provider] = _summarise_view(provider, value)
    return summary


def _summarise_view(provider: str, value: dict) -> dict:
    raw_windows = value.get("windows")
    headline = _headline_window(provider, raw_windows)
    used_percent = headline.get("used_percent") if headline is not None else None
    window_name = headline.get("name") if headline is not None else None
    age_seconds = value.get("age_seconds")
    age_usable = (
        isinstance(age_seconds, (int, float))
        and not isinstance(age_seconds, bool)
        and math.isfinite(float(age_seconds))
        and float(age_seconds) >= 0.0
    )
    return {
        "observed": True,
        "exhausted": bool(value.get("exhausted", False)),
        "status": str(value.get("status", "unknown")),
        "age_seconds": (float(age_seconds) if age_usable else None),
        "windows_observed": isinstance(raw_windows, list) and bool(raw_windows),
        "windows": _active_windows(raw_windows),
        "headline_window": window_name if isinstance(window_name, str) else None,
        "headline_used_percent": (
            float(used_percent) if isinstance(used_percent, (int, float)) else None
        ),
        "resets_at": headline.get("resets_at") if headline is not None else None,
    }


def _age_suffix(age_seconds: object) -> str:
    if not isinstance(age_seconds, (int, float)) or isinstance(age_seconds, bool):
        return ""
    return f", observed {age_seconds:g}s ago"


def _quota_detail(name: str, quota: dict, *, exhausted: bool = False) -> str:
    used = quota["headline_used_percent"]
    window = quota["headline_window"]
    if exhausted:
        detail = (
            f"{name} exhausted at {used}%"
            if used is not None
            else f"{name} exhausted at unknown usage"
        )
        if window is not None:
            detail += f" ({window})"
    elif used is None:
        detail = f"{name} unknown usage"
    elif window is None:
        detail = f"{name} {used}%"
    else:
        detail = f"{name} {used}% of {window}"
        preferred = _preferred_window_name(name)
        if window != preferred:
            detail += f" ({preferred} expired)"
    return detail + _age_suffix(quota.get("age_seconds"))


def _unobserved_age_detail(providers: object) -> str:
    if not isinstance(providers, dict):
        return ""
    details = []
    for name in ("codex", "claude"):
        value = providers.get(name)
        if not isinstance(value, dict):
            continue
        suffix = _age_suffix(value.get("age_seconds"))
        if suffix:
            details.append(f"{name}{suffix}")
    return "; ".join(details)


async def provider_quota_health() -> dict:
    """Report provider quota as an advisory health component."""
    fetched = await fetch_provider_quota()
    raw_providers = fetched.get("providers", {})
    providers = summarise(raw_providers)
    if not fetched.get("available", False):
        detail = fetched.get("reason", "broker unavailable")
        if providers:
            detail += "; " + "; ".join(
                _quota_detail(name, quota) for name, quota in providers.items()
            )
        return {
            "ok": True,
            "status": "unknown",
            "detail": detail,
            "providers": providers,
        }
    if not providers:
        detail = "no provider quota observed"
        age_detail = _unobserved_age_detail(raw_providers)
        if age_detail:
            detail += "; " + age_detail
        return {
            "ok": True,
            "status": "unknown",
            "detail": detail,
            "providers": providers,
        }

    exhausted = []
    for name, quota in providers.items():
        if not quota["exhausted"]:
            continue
        reset = quota["resets_at"] or "unknown reset"
        exhausted.append(
            f"{_quota_detail(name, quota, exhausted=True)}, resets at {reset}"
        )
    if exhausted:
        return {
            "ok": False,
            "status": "advisory",
            "detail": "; ".join(exhausted),
            "providers": providers,
        }
    return {
        "ok": True,
        "status": "ok",
        "detail": "; ".join(
            _quota_detail(name, quota) for name, quota in providers.items()
        ),
        "providers": providers,
    }
