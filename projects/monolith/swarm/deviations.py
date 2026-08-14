"""Mechanical, engine-recorded deviations from a pinned swarm plan."""

from __future__ import annotations


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
        budget = plan.get("budget_usd")
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
                        f"cost_usd: {spent}; pinned budget_usd: {ceiling}",
                        f"run spent {spent} against a pinned {ceiling} budget.",
                    )
                )

    return deviations
