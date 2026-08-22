"""Markdown reporting for probe JSONL results."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

from probe.spans import HOPS


def _median(values: list[float | int | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return statistics.median(present) if present else None


def token_total(result: dict) -> int | None:
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None
    values = []
    for keys in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
    ):
        value = next(
            (usage.get(key) for key in keys if usage.get(key) is not None), None
        )
        if isinstance(value, (int, float)):
            values.append(int(value))
    return sum(values) if values else None


def _number(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_report(results: list[dict]) -> str:
    """Render per-task and per-hop medians as GitHub-flavored Markdown."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for result in results:
        grouped[str(result.get("task", "unknown"))].append(result)

    lines = [
        "| task | reps | pass | median wall_s | median turns | median tokens |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for task, rows in sorted(grouped.items()):
        wall = _median([row.get("wall_s") for row in rows])
        turns = _median([row.get("num_turns") for row in rows])
        tokens = _median([token_total(row) for row in rows])
        lines.append(
            f"| {task} | {len(rows)} | {sum(bool(row.get('passed')) for row in rows)} "
            f"| {_number(wall)} | {_number(turns)} | {_number(tokens)} |"
        )

    lines.extend(
        [
            "",
            "| task | hop | median total_ms |",
            "|---|---|---:|",
        ]
    )
    for task, rows in sorted(grouped.items()):
        for hop in HOPS:
            totals = []
            for row in rows:
                span_buckets = row.get("span_buckets") or {}
                buckets = span_buckets.get("buckets", span_buckets)
                bucket = buckets.get(hop, {}) if isinstance(buckets, dict) else {}
                totals.append(
                    bucket.get("total_ms") if isinstance(bucket, dict) else None
                )
            lines.append(f"| {task} | {hop} | {_number(_median(totals))} |")
    return "\n".join(lines) + "\n"


def load_jsonl(path: Path) -> list[dict]:
    results = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise TypeError(f"line {line_number} in {path} is not a JSON object")
        results.append(value)
    return results
