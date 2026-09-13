"""Budget management for swarm runs."""

from __future__ import annotations


def effective_budget(plan: dict, attributes: dict | None = None) -> float | None:
    """Compute the pinned budget after applying the latest recorded raise.

    Args:
        plan: The pinned plan with an optional ``budget_usd`` value.
        attributes: Workflow attributes with an optional ``budget_raises`` list.

    Returns:
        The effective budget ceiling, or ``None`` when no budget is pinned.
    """
    base_budget = plan.get("budget_usd")
    if base_budget is None:
        return None
    attributes = attributes or {}
    budget_raises = attributes.get("budget_raises", [])
    if budget_raises and isinstance(budget_raises, list):
        latest_raise = budget_raises[-1]
        if isinstance(latest_raise, dict) and "to" in latest_raise:
            return float(latest_raise["to"])
    return float(base_budget)
