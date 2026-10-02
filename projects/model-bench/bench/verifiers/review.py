"""Seeded code-review verifier: grade a model's review against planted bugs.

The fixture is a real change with known bugs planted into it (the plants live in
task.yaml, never in the fixture). The model writes its findings to a JSON file as
``[{"file", "line", "description"}]``. A finding matches a planted bug when it names
the same file and a line within the bug's range, widened by ``tolerance``. Each bug
is matched at most once; a further finding inside an already-matched bug's window is
a duplicate and neither scores nor costs. Any other finding is a false positive.

score = max(0, (matched - fp_penalty * false_positives) / bugs), so a reviewer that
sprays findings at every line pays for the noise, and one that finds the obvious
bugs but misses the subtle ones lands in between.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bench.verifiers import VerifyResult, register


def _norm(path: str) -> str:
    path = path.strip().removeprefix("./")
    for prefix in ("a/", "b/"):
        path = path.removeprefix(prefix)
    return path


def _same_file(finding: str, bug: str) -> bool:
    # A model may cite the repo path (projects/monolith/chat_public/x.py) for a
    # fixture-relative bug path (chat_public/x.py); accept either direction.
    f, b = _norm(finding), _norm(bug)
    return f == b or f.endswith("/" + b) or b.endswith("/" + f)


def _line(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    m = re.match(r"\s*L?(\d+)", str(value))
    return int(m.group(1)) if m else None


def _findings(data) -> list[dict] | None:
    if isinstance(data, dict):
        data = data.get("findings")
    if not isinstance(data, list):
        return None
    return [f for f in data if isinstance(f, dict)]


@register("review-findings")
def verify(workdir: Path, args: dict) -> VerifyResult:
    name: str = args.get("file", "review.json")
    bugs: list[dict] = args["bugs"]
    tolerance: int = args.get("tolerance", 3)
    fp_penalty: float = args.get("fp_penalty", 0.25)
    threshold: float = args.get("pass_threshold", 1.0)

    path = workdir / name
    if not path.is_file():
        return VerifyResult(False, f"no {name} written", 0.0)
    try:
        findings = _findings(json.loads(path.read_text()))
    except json.JSONDecodeError as exc:
        return VerifyResult(False, f"{name} is not valid JSON: {exc}", 0.0)
    if findings is None:
        return VerifyResult(
            False, f"{name} must be a JSON list of {{file, line, description}}", 0.0
        )

    def window(bug: dict) -> tuple[int, int]:
        lo, hi = bug["lines"]
        return lo - tolerance, hi + tolerance

    matched: dict[str, int] = {}  # bug id -> index of the finding that claimed it
    false_positives = 0
    for i, finding in enumerate(findings):
        line = _line(finding.get("line"))
        file = str(finding.get("file", ""))
        hits = [
            b
            for b in bugs
            if line is not None
            and _same_file(file, b["file"])
            and window(b)[0] <= line <= window(b)[1]
        ]
        fresh = [b for b in hits if b["id"] not in matched]
        if fresh:
            # Closest unclaimed bug wins when windows overlap.
            best = min(fresh, key=lambda b: abs(sum(b["lines"]) / 2 - line))
            matched[best["id"]] = i
        elif not hits:
            false_positives += 1

    score = max(0.0, (len(matched) - fp_penalty * false_positives) / len(bugs))
    missed = [b["id"] for b in bugs if b["id"] not in matched]
    feedback = (
        f"matched {len(matched)}/{len(bugs)} planted bugs, "
        f"{false_positives} false positive(s)"
    )
    if missed:
        feedback += f"; missed: {', '.join(missed)}"
    return VerifyResult(score >= threshold, feedback, score)
