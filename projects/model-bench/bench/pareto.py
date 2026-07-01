from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import median

# Coarse-tier thresholds. Tunable; documented here so callers can import them.
ONE_SHOT_BAR = 0.8
RECOVER_BAR = 0.8


@dataclass
class ClassScore:
    pass1: float
    cost: float
    pass2: float = 0.0
    latency_ms: float = 0.0


def pareto_frontier(points: dict[str, tuple[float, float]]) -> set[str]:
    """Return the set of non-dominated model names.

    points maps model_id -> (pass1_rate, cost_usd).
    Higher pass1 is better; lower cost is better.

    A model M is dominated if there exists another model N such that:
      pass1_N >= pass1_M  AND  cost_N <= cost_M
    with at least one strict inequality.
    """
    dominated: set[str] = set()
    names = list(points.keys())
    for m in names:
        pass1_m, cost_m = points[m]
        for n in names:
            if n == m:
                continue
            pass1_n, cost_n = points[n]
            if (
                pass1_n >= pass1_m
                and cost_n <= cost_m
                and (pass1_n > pass1_m or cost_n < cost_m)
            ):
                dominated.add(m)
                break
    return set(names) - dominated


def qualifies(cand: ClassScore, anchor: ClassScore) -> bool:
    """Return True if cand meets or exceeds anchor pass1 at strictly lower cost."""
    return cand.pass1 >= anchor.pass1 and cand.cost < anchor.cost


def coarse_tier(pass1_first: float, pass_any: float) -> str:
    """Classify a model result into a coarse tier.

    Thresholds are module-level constants (ONE_SHOT_BAR, RECOVER_BAR) and are
    intentionally coarse -- ties within a tier are ties.
    """
    if pass1_first >= ONE_SHOT_BAR:
        return "one-shots"
    if pass_any >= RECOVER_BAR:
        return "needs-repair"
    return "can't"


def aggregate_by_class(
    cells: list,
    task_class_of: dict[str, str],
) -> dict[str, dict[str, ClassScore]]:
    """Group ResultCell-like objects by (model_id, task_class) and compute scores.

    Args:
        cells: sequence of ResultCell (or duck-typed equivalent) with attributes
               task_id, model_id, first_attempt_passed, outcome, cost_usd,
               total_latency_ms.
        task_class_of: mapping from task_id to task class string.

    Returns:
        Nested dict: model_id -> task_class -> ClassScore.
        pass1 = fraction with first_attempt_passed.
        pass2 (stored in ClassScore.pass2) = fraction with outcome in
              ("pass@1", "pass@2") i.e. passed on any attempt.
        cost = mean cost_usd across cells.
        latency_ms = median total_latency_ms across cells.
    """
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for cell in cells:
        cls = task_class_of.get(cell.task_id)
        if cls is None:
            continue
        groups[(cell.model_id, cls)].append(cell)

    result: dict[str, dict[str, ClassScore]] = defaultdict(dict)
    for (model_id, cls), group in groups.items():
        if not group:
            continue
        n = len(group)
        pass1_rate = sum(1 for c in group if c.first_attempt_passed) / n
        pass_any_rate = sum(1 for c in group if c.outcome in ("pass@1", "pass@2")) / n
        mean_cost = sum(c.cost_usd for c in group) / n
        latencies = [c.total_latency_ms for c in group]
        med_latency = float(median(latencies)) if latencies else 0.0
        result[model_id][cls] = ClassScore(
            pass1=pass1_rate,
            cost=mean_cost,
            pass2=pass_any_rate,
            latency_ms=med_latency,
        )

    return dict(result)
