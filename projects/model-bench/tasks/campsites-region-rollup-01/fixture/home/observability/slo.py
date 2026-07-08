from __future__ import annotations

import re


def compute_status(current: float | None, target: float | None) -> str:
    if current is None or target is None:
        return "healthy"
    if current >= target:
        return "healthy"
    if current >= target - 0.5:
        return "warning"
    return "degraded"


def compute_budget(
    current: float, target: float, window_days: int, elapsed_days: int | None = None
) -> dict:
    if elapsed_days is None:
        elapsed_days = window_days

    window_minutes = window_days * 24 * 60
    budget_minutes = window_minutes * (1 - target / 100)
    error_fraction = 1 - current / 100
    consumed_minutes = error_fraction * elapsed_days * 24 * 60
    remaining = max(0.0, budget_minutes - consumed_minutes)
    consumed_pct = (
        min(100, round(consumed_minutes / budget_minutes * 100))
        if budget_minutes > 0
        else 0
    )

    return {
        "consumed": consumed_pct,
        "elapsed": round(elapsed_days / window_days * 100) if window_days > 0 else 0,
        "remaining": f"{remaining:.1f} min" if remaining > 0 else "0 min",
        "window": f"{window_days}d",
    }


def compute_brief(availability: float | None, metrics: dict[str, str]) -> str:
    parts = []
    if availability is not None:
        parts.append(f"{availability:.2f}%" if availability < 100 else "100%")
    if "rps" in metrics:
        parts.append(f"{metrics['rps']} rps")
    elif not parts and metrics:
        first_val = next(iter(metrics.values()))
        parts.append(str(first_val))
    return " · ".join(parts) if parts else "healthy"


_STATUS_ORDER = {"healthy": 0, "warning": 1, "degraded": 2}


def aggregate_group(children: list[dict], target: float, window_days: int) -> dict:
    availabilities = [
        c["slo"]["current"]
        for c in children
        if c.get("slo") and c["slo"].get("current") is not None
    ]
    current = min(availabilities) if availabilities else None

    statuses = [c.get("status", "healthy") for c in children]
    worst_status = max(statuses, key=lambda s: _STATUS_ORDER.get(s, 0))

    all_metrics = []
    for c in children:
        for m in c.get("metrics", []):
            all_metrics.append(m)

    aggregated_metrics = []
    rps_total = 0.0
    has_rps = False
    p99_max = 0.0
    p99_label = ""
    has_p99 = False

    for m in all_metrics:
        k, v = m["k"], m["v"]
        if k == "rps":
            has_rps = True
            try:
                rps_total += float(v)
            except (ValueError, TypeError):
                pass
        elif k in ("p99", "latency"):
            has_p99 = True
            num = re.sub(r"[^\d.]", "", str(v))
            try:
                val = float(num)
                if val > p99_max:
                    p99_max = val
                    p99_label = str(v)
            except (ValueError, TypeError):
                pass

    if has_rps:
        aggregated_metrics.append({"k": "rps", "v": f"{rps_total:.1f}"})
    if has_p99:
        aggregated_metrics.append({"k": "latency", "v": p99_label})

    result: dict = {"status": worst_status, "metrics": aggregated_metrics}

    if current is not None:
        result["slo"] = {"target": target, "current": current}
        result["budget"] = compute_budget(current, target, window_days)
        metrics_dict = {m["k"]: m["v"] for m in aggregated_metrics}
        result["brief"] = compute_brief(current, metrics_dict)
    else:
        result["brief"] = "healthy"

    return result
