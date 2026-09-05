"""Cached token broker quota observations and advisory health."""

from __future__ import annotations

import logging
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
    return {
        "available": True,
        "providers": payload["providers"],
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


def _preferred_window_name(provider: str) -> str:
    return "primary" if provider == "codex" else "5h"


def summarise(providers: dict) -> dict:
    """Select the actionable quota window for each observed provider."""
    summary = {}
    for provider in ("codex", "claude"):
        value = providers.get(provider)
        if not isinstance(value, dict) or not value.get("observed", False):
            continue
        headline = _headline_window(provider, value.get("windows"))
        used_percent = headline.get("used_percent") if headline is not None else None
        window_name = headline.get("name") if headline is not None else None
        age_seconds = value.get("age_seconds")
        summary[provider] = {
            "observed": True,
            "exhausted": bool(value.get("exhausted", False)),
            "status": str(value.get("status", "unknown")),
            "age_seconds": (
                float(age_seconds)
                if isinstance(age_seconds, (int, float))
                and not isinstance(age_seconds, bool)
                else None
            ),
            "headline_window": window_name if isinstance(window_name, str) else None,
            "headline_used_percent": (
                float(used_percent) if isinstance(used_percent, (int, float)) else None
            ),
            "resets_at": headline.get("resets_at") if headline is not None else None,
        }
    return summary


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
