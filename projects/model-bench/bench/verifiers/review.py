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


def _ranges(target: dict) -> list[tuple[int, int]]:
    """A bug or decoy's line ranges: ``[lo, hi]`` or a list of them, for a defect
    that shows at more than one place (a loop cursor and the query it pages)."""
    lines = target["lines"]
    if lines and isinstance(lines[0], list):
        return [(lo, hi) for lo, hi in lines]
    lo, hi = lines
    return [(lo, hi)]


def _distance(line: int, target: dict) -> int:
    return min(
        0 if lo <= line <= hi else min(abs(line - lo), abs(line - hi))
        for lo, hi in _ranges(target)
    )


@register("review-findings")
def verify(workdir: Path, args: dict) -> VerifyResult:
    name: str = args.get("file", "review.json")
    bugs: list[dict] = args["bugs"]
    # Decoys: code that looks wrong but is correct in context. Flagging one is a
    # false positive with its own (usually higher) penalty.
    decoys: list[dict] = args.get("decoys", [])
    tolerance: int = args.get("tolerance", 3)
    fp_penalty: float = args.get("fp_penalty", 0.25)
    decoy_penalty: float = args.get("decoy_penalty", fp_penalty)
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

    targets = [("bug", b) for b in bugs] + [("decoy", d) for d in decoys]
    matched: set[str] = set()
    decoy_hits: list[str] = []
    false_positives = 0
    for finding in findings:
        line = _line(finding.get("line"))
        file = str(finding.get("file", ""))
        near = [
            (_distance(line, t), kind, t)
            for kind, t in targets
            if line is not None
            and _same_file(file, t["file"])
            and _distance(line, t) <= tolerance
        ]
        if not near:
            false_positives += 1
            continue
        # The nearest target owns the finding; an unclaimed bug beats a claimed
        # one at equal distance, so two findings can split adjacent bugs.
        near.sort(key=lambda n: (n[0], n[1] == "bug" and n[2]["id"] in matched))
        _, kind, target = near[0]
        if kind == "decoy":
            decoy_hits.append(target["id"])
        elif target["id"] not in matched:
            matched.add(target["id"])
        # A repeat finding on an already-matched bug neither scores nor costs.

    penalty = fp_penalty * false_positives + decoy_penalty * len(decoy_hits)
    score = max(0.0, (len(matched) - penalty) / len(bugs))
    missed = [b["id"] for b in bugs if b["id"] not in matched]
    feedback = (
        f"matched {len(matched)}/{len(bugs)} planted bugs, "
        f"{false_positives} false positive(s)"
    )
    if decoys:
        feedback += f", {len(decoy_hits)} decoy hit(s)"
        if decoy_hits:
            feedback += f" ({', '.join(sorted(set(decoy_hits)))})"
    if missed:
        feedback += f"; missed: {', '.join(missed)}"
    return VerifyResult(score >= threshold, feedback, score)
