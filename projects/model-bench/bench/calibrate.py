"""Anchor-ladder calibration: admit a task to the frontier tier (#6701).

A frontier task should separate the Claude anchors: Haiku fails, Sonnet gets part of
the way, Opus succeeds. `bench calibrate` runs each anchor on a task several times
through the same Claude Code path as `bench run` (pinned with --model), takes each
run's graded score (1/0 for a binary verifier), and checks the means against the
ladder thresholds. `--from-json` records scores produced elsewhere (e.g. subagent runs
graded by hand) without invoking the CLI.

The result is written to the task's `calibration:` block, which is provenance only:
the cell key covers prompt, fixture, verifier and params, so recording calibration
never invalidates cached cells.
"""

from __future__ import annotations

import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import mean

import yaml

from bench import claude_code
from bench.schema import ModelSpec, TaskSpec
from bench.verifiers import get_verifier

# Short names on the command line -> anchor registry ids.
ANCHOR_ALIASES = {
    "haiku": "anthropic/claude-haiku-4.5",
    "sonnet": "anthropic/claude-sonnet-5.5-cc",
    "opus": "anthropic/claude-opus-5.5",
}

# Feedback prefixes for a run that never reached the grader.
_UNGRADED_MARKERS = (
    "[harness error]",
    "[anchor harness error]",
    "[claude CLI reported is_error]",
    "[verifier setup]",
)

# Default ladder. A task may override any of these under `calibration.ladder`.
DEFAULT_LADDER = {
    "haiku_max": 0.4,
    "sonnet_min": 0.35,
    "sonnet_max": 0.8,
    "opus_min": 0.85,
}


def resolve_anchors(reg: list[ModelSpec], names: list[str]) -> dict[str, ModelSpec]:
    """Map short names (haiku/sonnet/opus) or full ids to anchor registry rows."""
    by_id = {m.id: m for m in reg}
    out: dict[str, ModelSpec] = {}
    for name in names:
        model_id = ANCHOR_ALIASES.get(name, name)
        model = by_id.get(model_id)
        if model is None or model.provider != "claude-code":
            raise ValueError(f"{name!r} is not a claude-code anchor in models.yaml")
        out[name] = model
    return out


def run_ladder(
    task: TaskSpec,
    fixture_dir: Path,
    anchors: dict[str, ModelSpec],
    *,
    reps: int,
    jobs: int,
) -> dict[str, list[float | None]]:
    """Run every anchor x rep and return {name: [score per run]}.

    A run that errored before grading (CLI failure, harness error) records None so it
    is visible but does not count as a zero.
    """
    verify = get_verifier(task.verifier.kind)

    def one(name: str) -> float | None:
        model = anchors[name]
        cell = claude_code.run_anchor_agent_cell(
            task_id=task.id,
            task_version=task.version,
            model_id=model.id,
            content_hash="calibrate",
            fixture_dir=fixture_dir,
            task_prompt=task.prompt,
            verify=verify,
            verifier_args=task.verifier.args,
            cli_model_name=claude_code.cli_model(model.id, model.api_model),
        )
        feedback = cell.attempts[0].feedback
        if any(m in feedback for m in _UNGRADED_MARKERS):
            return None
        attempt = cell.attempts[0]
        if attempt.score is not None:
            return attempt.score
        return 1.0 if attempt.passed else 0.0

    order = [name for _ in range(reps) for name in anchors]
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(one, order))
    scores: dict[str, list[float | None]] = {name: [] for name in anchors}
    for name, score in zip(order, results):
        scores[name].append(score)
    return scores


def summarize(scores: dict[str, list[float | None]]) -> dict[str, dict]:
    out = {}
    for name, runs in scores.items():
        graded = [s for s in runs if s is not None]
        out[name] = {
            "runs": [None if s is None else round(s, 4) for s in runs],
            "mean": round(mean(graded), 4) if graded else None,
            "min": round(min(graded), 4) if graded else None,
            "max": round(max(graded), 4) if graded else None,
            "errored": len(runs) - len(graded),
        }
    return out


def check_ladder(summary: dict[str, dict], ladder: dict) -> tuple[bool, list[str]]:
    """Return (admitted, reasons). Every anchor must have at least one graded run."""
    t = {**DEFAULT_LADDER, **(ladder or {})}
    reasons: list[str] = []
    means = {}
    for name in ("haiku", "sonnet", "opus"):
        m = summary.get(name, {}).get("mean")
        if m is None:
            reasons.append(f"{name}: no graded runs")
        means[name] = m
    if reasons:
        return False, reasons
    if means["haiku"] > t["haiku_max"]:
        reasons.append(f"haiku mean {means['haiku']:.2f} > {t['haiku_max']}")
    if not t["sonnet_min"] <= means["sonnet"] <= t["sonnet_max"]:
        reasons.append(
            f"sonnet mean {means['sonnet']:.2f} outside "
            f"[{t['sonnet_min']}, {t['sonnet_max']}]"
        )
    if means["opus"] < t["opus_min"]:
        reasons.append(f"opus mean {means['opus']:.2f} < {t['opus_min']}")
    if not means["haiku"] < means["sonnet"] < means["opus"]:
        reasons.append("means are not strictly increasing haiku < sonnet < opus")
    return not reasons, reasons


def render(summary: dict[str, dict], admitted: bool, reasons: list[str]) -> str:
    def fmt(v):
        return "n/a" if v is None else f"{v:.2f}"

    lines = [
        "| model | mean | min | max | runs | errored |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, s in summary.items():
        runs = ", ".join(fmt(r) for r in s["runs"])
        lines.append(
            f"| {name} | {fmt(s['mean'])} | {fmt(s['min'])} | {fmt(s['max'])} "
            f"| {runs} | {s['errored']} |"
        )
    verdict = "ADMIT (frontier)" if admitted else "REJECT: " + "; ".join(reasons)
    lines.append("")
    lines.append(f"verdict: {verdict}")
    return "\n".join(lines)


def load_scores_json(path: Path) -> dict[str, list[float | None]]:
    """Read externally produced scores: {"haiku": [0.3, 0.5], "sonnet": [...], ...}."""
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise TypeError("--from-json expects an object of {model: [scores]}")
    return {
        name: [None if s is None else float(s) for s in runs]
        for name, runs in data.items()
    }


def write_calibration(task_file: Path, record: dict) -> None:
    """Replace the top-level `calibration:` block in task.yaml, keeping the rest of
    the file (and its comments) byte for byte."""
    lines = task_file.read_text().splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    for line in lines:
        if line.startswith("calibration:"):
            skipping = True
            continue
        if skipping and (line.startswith((" ", "\t")) or not line.strip()):
            continue
        skipping = False
        kept.append(line)
    text = "".join(kept)
    if text and not text.endswith("\n"):
        text += "\n"
    block = yaml.safe_dump({"calibration": record}, sort_keys=False, width=100)
    task_file.write_text(text + block)


def calibration_record(
    summary: dict[str, dict],
    admitted: bool,
    reasons: list[str],
    *,
    source: str,
    ladder: dict | None,
    today: datetime.date | None = None,
) -> dict:
    record = {
        "date": (today or datetime.datetime.now(datetime.UTC).date()).isoformat(),
        "source": source,
        "scores": summary,
        "verdict": "admit" if admitted else "reject",
    }
    if reasons:
        record["reasons"] = reasons
    if ladder:
        record["ladder"] = ladder
    return record
