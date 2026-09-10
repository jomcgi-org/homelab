"""Mechanical, engine-recorded deviations from a pinned swarm plan."""

from __future__ import annotations

from swarm.budget import effective_budget


def _deviation(code: str, node_key: str, evidence: str, text: str) -> dict:
    return {"code": code, "node_key": node_key, "evidence": evidence, "text": text}


def compute_deviations(run: dict) -> list[dict]:
    """Return deviations that can be computed from the composed run state."""
    deviations = []
    plan = run.get("plan") or {}
    pinned = plan.get("pinned") is True
    nodes = run.get("nodes") or []
    by_key = {node.get("key"): node for node in nodes if node.get("key")}
    implement = by_key.get("implement") or {}
    implement_attempts = implement.get("attempts") or []

    if (
        pinned
        and run.get("state") == "escalated"
        and plan.get("max_attempts") is not None
        and len(implement_attempts) == plan["max_attempts"]
    ):
        spent = len(implement_attempts)
        maximum = plan["max_attempts"]
        deviations.append(
            _deviation(
                "attempts_exhausted",
                "implement",
                f"spent attempts: {spent}; pinned max_attempts: {maximum}",
                f"implement exhausted its {maximum} pinned attempts.",
            )
        )

    for node in nodes:
        attempts = node.get("attempts") or []
        if len(attempts) <= 1:
            continue
        finding_codes = [
            attempt.get("finding", {}).get("code")
            for attempt in attempts[:-1]
            if isinstance(attempt.get("finding"), dict)
            and attempt.get("finding", {}).get("code")
        ]
        finding = finding_codes[-1] if finding_codes else None
        evidence = f"attempts: {len(attempts)}"
        if finding:
            evidence += f"; retry finding code: {finding}"
            text = f"{node.get('label', node.get('key'))} took {len(attempts)} attempts after {finding}."
        else:
            text = (
                f"{node.get('label', node.get('key'))} took {len(attempts)} attempts."
            )
        deviations.append(_deviation("retry_taken", node["key"], evidence, text))

    if pinned:
        expected_models = {
            "implement": plan.get("implementer_model"),
            "review": plan.get("reviewer_model"),
        }
        for node in nodes:
            expected = expected_models.get(node.get("key"))
            if expected is None:
                continue
            for attempt in node.get("attempts") or []:
                recorded = attempt.get("model")
                if recorded is None or recorded == expected:
                    continue
                deviations.append(
                    _deviation(
                        "model_mismatch",
                        node["key"],
                        f"recorded model: {recorded}; pinned model: {expected}",
                        f"{node.get('label', node.get('key'))} used {recorded} instead of pinned {expected}.",
                    )
                )

        cost = run.get("cost_usd")
        attributes = run.get("attributes") or {}
        budget = effective_budget(plan, attributes)
        if cost is not None and budget is not None:
            try:
                exceeded = float(cost) > float(budget)
            except (TypeError, ValueError):
                exceeded = False
            if exceeded:
                # Money is formatted here, not left as a raw float. cost_usd is
                # a sum of per-turn floats, so it arrives as 0.30000000000000004
                # and the client renders these strings verbatim.
                spent = f"${float(cost):.2f}"
                ceiling = f"${float(budget):.2f}"
                deviations.append(
                    _deviation(
                        "budget_exceeded",
                        "run",
                        f"cost_usd: {spent}; effective budget_usd: {ceiling}",
                        f"run spent {spent} against effective {ceiling} budget.",
                    )
                )

    return deviations


# swarm.graph owns the same tuple, but importing it here would drag core.db and
# the engine into every consumer of this pure module.
_SETTLED = ("succeeded", "failed", "escalated", "cancelled")
FACTORY_DEVIATION_CODES = (
    "loop_insert_refused",
    "initial_plan",
    "review_rounds_exhausted",
    "node_escalated",
    "node_failed",
    "graph_exhausted",
)


def factory_deviation(
    nodes: list[dict],
    runs: list[dict],
    ready: list[dict],
    *,
    review_rounds_used: int,
    max_review_rounds: int,
    pending_review: str | None = None,
    loop_refusal: str | None = None,
) -> dict | None:
    """Name why a factory plan needs its planner, or None while it runs itself.

    The engine owns mechanical progress: a ready node is dispatched, and a
    review that requested changes opens a bounded correction round. Returning
    None is the ordinary case and means no planner turn is spent. Every other
    return is a deviation from the pinned plan that only the planner can
    resolve, named with the graph evidence that produced it.
    """
    work = [node for node in nodes if not node["node_key"].startswith("conductor_")]
    if loop_refusal is not None:
        return _deviation(
            "loop_insert_refused",
            pending_review or "run",
            f"review round refusal: {loop_refusal}",
            "The engine could not open the next review round.",
        )
    if not work:
        return _deviation(
            "initial_plan",
            "run",
            f"work nodes: 0; graph nodes: {len(nodes)}",
            "No plan has been applied to this task yet.",
        )
    if pending_review is not None and review_rounds_used >= max_review_rounds:
        return _deviation(
            "review_rounds_exhausted",
            pending_review,
            f"review rounds used: {review_rounds_used}; "
            f"max_review_rounds: {max_review_rounds}",
            f"{pending_review} requested changes after "
            f"{review_rounds_used} engine-owned correction rounds.",
        )
    ready_keys = {node["node_key"] for node in ready}
    attempts_by_node: dict[str, list[dict]] = {}
    for run in runs:
        attempts_by_node.setdefault(run["node_key"], []).append(run)
    for node in work:
        key = node["node_key"]
        attempts = attempts_by_node.get(key) or []
        if not attempts or key in ready_keys:
            continue
        if any(run["status"] == "succeeded" for run in attempts):
            continue
        if any(run["status"] == "escalated" for run in attempts):
            return _deviation(
                "node_escalated",
                key,
                f"attempts: {len(attempts)}",
                f"{key} escalated to the conductor rather than delivering.",
            )
        if all(run["status"] in _SETTLED for run in attempts):
            return _deviation(
                "node_failed",
                key,
                f"attempts: {len(attempts)}; max_attempts: {node.get('max_attempts')}",
                f"{key} failed and has no runnable retry.",
            )
    if not ready:
        return _deviation(
            "graph_exhausted",
            "run",
            f"work nodes: {len(work)}; ready nodes: 0",
            "Every planned node settled and the task is not delivered.",
        )
    return None
