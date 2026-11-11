"""Optional operator-owned refill policy, normalized for legacy comparisons."""

from __future__ import annotations

DEFAULTS = {
    "enabled": False,
    "max_findings": 100,
    "min_eligible": 25,
    "cooldown_minutes": 15,
    "min_new_issues": 3,
    "breaker_consecutive_low_yield": 3,
    "max_per_24_hours": 24,
    "model": "astra",
    "effort": "high",
}


def normalize(policy: dict) -> dict:
    """Omission is inert, including on policies predating the Astra model."""
    value = policy.get("backlog_refill", {})
    if not isinstance(value, dict) or not set(value) <= set(DEFAULTS):
        raise ValueError("invalid backlog_refill")
    result = {**DEFAULTS, **value}
    if type(result["enabled"]) is not bool:
        raise ValueError("invalid backlog_refill enabled")
    for key, low, high in (
        ("min_eligible", 1, 1000),
        ("max_findings", 1, 100),
        ("cooldown_minutes", 1, 1440),
        ("min_new_issues", 0, 100),
        ("breaker_consecutive_low_yield", 1, 10),
        ("max_per_24_hours", 1, 24),
    ):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"invalid backlog_refill {key}")
    if not isinstance(result["model"], str) or (
        (result["enabled"] or "model" in value)
        and result["model"] not in policy.get("allowed_models", [])
    ):
        raise ValueError("backlog_refill model is not allowed")
    if not isinstance(result["effort"], str) or result["effort"] not in (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ):
        raise ValueError("invalid backlog_refill effort")
    return result
