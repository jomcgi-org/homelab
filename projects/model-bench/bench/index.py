"""jomcgi-agent-index: rank models for each factory role against Joe's criteria.

A model is scored on a handful of axes (correctness, frontier, judgement,
security, review, norms, and an optional pairwise-judge rating). Each role
weights the axes differently; `index.yaml` holds the axes and the weights, so
re-weighting needs no re-run. An axis a model has no cells for is skipped and
the remaining weights renormalise, rather than the gap counting as a zero.

Uncertainty is a bootstrap over the model's own tasks: resample its graded
cells with replacement, recompute the role index, and report the 2.5/97.5
percentiles. An axis measured on fewer than `min_n` tasks is flagged, because
at n=1-2 the difference between 0.8 and 0.9 is noise.

Per role the output also names the cheapest and the fastest model whose index
is within `tolerance` of the best, which is the routing question: the
shortest wall time and lowest cost for a positive outcome.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

import yaml

from bench.schema import ResultCell

FLOOR_TIERS = frozenset({"easy", "standard"})
CAPABILITY_TIERS = frozenset({"hard", "frontier"})
AXES = ("correctness", "frontier", "judgement", "security", "review", "norms", "judge")
# Axes measured from tasks tagged with the same name in task.yaml `axes:`.
TAGGED_AXES = ("judgement", "security", "review")


@dataclass
class IndexConfig:
    roles: dict[str, dict[str, float]]
    min_n: int = 3
    tolerance: float = 0.05
    bootstrap: int = 1000
    seed: int = 0
    descriptions: dict[str, str] = field(default_factory=dict)


def load_config(path: Path) -> IndexConfig:
    raw = yaml.safe_load(path.read_text()) or {}
    roles = raw.get("roles") or {}
    for role, weights in roles.items():
        unknown = set(weights) - set(AXES)
        if unknown:
            raise ValueError(f"role {role!r} weights unknown axes {sorted(unknown)}")
    return IndexConfig(
        roles={r: {a: float(w) for a, w in ws.items()} for r, ws in roles.items()},
        min_n=int(raw.get("min_n", 3)),
        tolerance=float(raw.get("tolerance", 0.05)),
        bootstrap=int(raw.get("bootstrap", 1000)),
        seed=int(raw.get("seed", 0)),
        descriptions={
            a: str(d.get("description", "")) if isinstance(d, dict) else str(d)
            for a, d in (raw.get("axes") or {}).items()
        },
    )


def load_judge_ratings(path: Path) -> dict[str, float]:
    """Read pairwise-judge ratings: {model: rating} or a leaderboard JSON whose
    models carry `judge_rating`. Missing or null ratings are skipped."""
    import json

    data = json.loads(path.read_text())
    if isinstance(data, dict) and isinstance(data.get("models"), list):
        return {
            m["id"]: float(m["judge_rating"])
            for m in data["models"]
            if m.get("judge_rating") is not None
        }
    out: dict[str, float] = {}
    for mid, v in (data or {}).items():
        rating = v.get("judge_rating") if isinstance(v, dict) else v
        if rating is not None:
            out[mid] = float(rating)
    return out


def _score(cell: ResultCell) -> float:
    score = cell.first_attempt_score
    if score is not None:
        return score
    return 1.0 if cell.first_attempt_passed else 0.0


def axis_scores(
    cells: list[ResultCell],
    tier_of: dict[str, str],
    axes_of: dict[str, list[str]],
) -> dict[str, tuple[float | None, int]]:
    """Per-axis (score, n_tasks) for one model's graded cells. The judge axis is
    not cell-based and is filled in by the caller."""

    def avg(values: list[float]) -> tuple[float | None, int]:
        return (float(mean(values)) if values else None, len(values))

    floor = [c for c in cells if tier_of.get(c.task_id) in FLOOR_TIERS]
    capability = [c for c in cells if tier_of.get(c.task_id) in CAPABILITY_TIERS]
    out = {
        "correctness": avg([1.0 if c.first_attempt_passed else 0.0 for c in floor]),
        "frontier": avg([_score(c) for c in capability]),
        "norms": avg(
            [
                c.norms_score
                for c in cells
                if c.first_attempt_passed and c.norms_score is not None
            ]
        ),
    }
    for axis in TAGGED_AXES:
        out[axis] = avg(
            [_score(c) for c in cells if axis in axes_of.get(c.task_id, [])]
        )
    return out


def role_index(
    scores: dict[str, float | None], weights: dict[str, float]
) -> tuple[float | None, float]:
    """Weighted mean over the axes that have a score; returns (index, coverage),
    coverage being the share of the role's weight that was measurable."""
    total = sum(weights.values())
    used = {a: w for a, w in weights.items() if scores.get(a) is not None and w > 0}
    if not used or total <= 0:
        return None, 0.0
    weight = sum(used.values())
    value = sum(scores[a] * w for a, w in used.items()) / weight
    return value, weight / total


def _normalise(ratings: dict[str, float]) -> dict[str, float]:
    if not ratings:
        return {}
    lo, hi = min(ratings.values()), max(ratings.values())
    if hi == lo:
        return {m: 1.0 for m in ratings}
    return {m: (r - lo) / (hi - lo) for m, r in ratings.items()}


def build_index(
    groups: dict[str, list[ResultCell]],
    *,
    tier_of: dict[str, str],
    axes_of: dict[str, list[str]],
    config: IndexConfig,
    stats: dict[str, dict] | None = None,
    judge_ratings: dict[str, float] | None = None,
    anchor_ids: set[str] | frozenset[str] = frozenset(),
) -> dict:
    """The full index block: per-model axes and role indices with CIs, plus per
    role the best, cheapest-within-tolerance and fastest-within-tolerance model.

    `groups` maps a model to its cells (harness-error cells are dropped here).
    `stats` is the per-model agentic aggregate (cost, mean_latency_ms) the
    leaderboard already computes.
    """
    stats = stats or {}
    judge = _normalise(judge_ratings or {})
    rng = random.Random(config.seed)
    models: dict[str, dict] = {}
    for model_id in sorted(groups):
        cells = [c for c in groups[model_id] if not c.is_harness_error]
        if not cells:
            continue
        measured = axis_scores(cells, tier_of, axes_of)
        scores = {a: v for a, (v, _) in measured.items()}
        counts = {a: n for a, (_, n) in measured.items()}
        if model_id in judge:
            scores["judge"] = judge[model_id]
            counts["judge"] = None
        low_n = sorted(
            a for a, n in counts.items() if n is not None and 0 < n < config.min_n
        )
        # Bootstrap over this model's tasks. The judge axis is a fixed input.
        samples: dict[str, list[float]] = {r: [] for r in config.roles}
        for _ in range(config.bootstrap):
            draw = [rng.choice(cells) for _ in cells]
            sample = {a: v for a, (v, _) in axis_scores(draw, tier_of, axes_of).items()}
            if "judge" in scores:
                sample["judge"] = scores["judge"]
            for role, weights in config.roles.items():
                value, _ = role_index(sample, weights)
                if value is not None:
                    samples[role].append(value)
        roles = {}
        for role, weights in config.roles.items():
            value, coverage = role_index(scores, weights)
            ci = _percentiles(samples[role])
            roles[role] = {
                "index": _round(value),
                "ci": [_round(ci[0]), _round(ci[1])] if ci else None,
                "coverage": round(coverage, 3),
            }
        s = stats.get(model_id, {})
        models[model_id] = {
            "role": "anchor" if model_id in anchor_ids else "candidate",
            "axes": {a: _round(scores.get(a)) for a in AXES},
            "axis_n": {a: counts.get(a) for a in AXES},
            "low_n": low_n,
            "roles": roles,
            "cost_usd": s.get("cost"),
            "mean_latency_ms": s.get("mean_latency_ms"),
        }

    picks = {}
    for role in config.roles:
        ranked = [
            (mid, m["roles"][role]["index"])
            for mid, m in models.items()
            if m["roles"][role]["index"] is not None
        ]
        if not ranked:
            picks[role] = {"best": None, "cheapest": None, "fastest": None}
            continue
        best_id, best = max(ranked, key=lambda r: (r[1], r[0]))
        near = [mid for mid, v in ranked if v >= best - config.tolerance]

        def pick(metric: str, near: list[str] = near) -> str | None:
            with_metric = [mid for mid in near if models[mid].get(metric) is not None]
            if not with_metric:
                return None
            return min(with_metric, key=lambda mid: (models[mid][metric], mid))

        picks[role] = {
            "best": best_id,
            "cheapest": pick("cost_usd"),
            "fastest": pick("mean_latency_ms"),
        }

    return {
        "roles": {r: dict(w) for r, w in config.roles.items()},
        "min_n": config.min_n,
        "tolerance": config.tolerance,
        "judge": bool(judge),
        "models": models,
        "picks": picks,
    }


def _percentiles(values: list[float]) -> tuple[float, float] | None:
    if not values:
        return None
    ordered = sorted(values)

    def at(q: float) -> float:
        k = (len(ordered) - 1) * q
        lo = int(k)
        hi = min(lo + 1, len(ordered) - 1)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)

    return at(0.025), at(0.975)


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 4)


def render_index_markdown(block: dict, names: dict[str, str] | None = None) -> str:
    """Compact per-role section for reports/leaderboard.md."""
    names = names or {}
    lines = [
        "## jomcgi-agent-index",
        "",
        (
            "Per-role index from `index.yaml` weights over the axes; 95% bootstrap "
            f"CI over tasks. `*` marks an axis measured on fewer than {block['min_n']} "
            "tasks. Picks are the cheapest and fastest model within "
            f"{block['tolerance']} of the best."
        ),
        "",
    ]
    models = block["models"]
    for role, weights in block["roles"].items():
        w = ", ".join(f"{a} {v:g}" for a, v in weights.items() if v)
        lines += [f"### {role}", "", f"Weights: {w}.", ""]
        rows = sorted(
            (
                (mid, m)
                for mid, m in models.items()
                if m["roles"][role]["index"] is not None
            ),
            key=lambda r: -r[1]["roles"][role]["index"],
        )
        if not rows:
            lines += ["No scored models yet.", ""]
            continue
        lines.append("| Model | index | 95% CI | coverage | low n |")
        lines.append("| --- | --- | --- | --- | --- |")
        for mid, m in rows:
            r = m["roles"][role]
            ci = f"{r['ci'][0]:.2f}-{r['ci'][1]:.2f}" if r["ci"] else "n/a"
            low = ", ".join(m["low_n"]) + " *" if m["low_n"] else ""
            lines.append(
                f"| {names.get(mid, mid)} | {r['index']:.3f} | {ci} "
                f"| {r['coverage']:.0%} | {low} |"
            )
        p = block["picks"][role]
        lines += [
            "",
            (
                f"Best: {p['best'] or 'n/a'}. Cheapest near best: "
                f"{p['cheapest'] or 'n/a'}. Fastest near best: {p['fastest'] or 'n/a'}."
            ),
            "",
        ]
    return "\n".join(lines)
