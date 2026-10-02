"""Speedup verifier: grade a performance change by bucketed speedup over the original.

The task carries the original module source (`baseline`) and a hidden harness
script. The verifier drops both into the workdir and runs the harness on the
venv python. The harness owns correctness and timing: it must check that the
candidate's output matches the baseline's (on edge cases and on the benchmark
inputs) and time both in the same process, interleaved, on fresh inputs per
run so cross-call caching cannot help. Its last stdout line is JSON:
{"ok": bool, "speedup": float, "detail": str}.

Wall-time ratios are noisy, so the score comes from coarse `buckets` rather
than the raw ratio: [[min_speedup, score], ...]. The highest bucket whose
min_speedup the measured speedup clears wins; below the first bucket scores 0.
Any correctness failure scores 0, however fast.
"""

from __future__ import annotations

import json
from pathlib import Path

from bench.verifiers import VerifyResult, register
from bench.verifiers.pytest import _venv_python
from bench.verifiers.sandbox import run_sandboxed

HARNESS_NAME = "_speedup_harness.py"


def _bucket_score(speedup: float, buckets: list) -> float:
    score = 0.0
    for min_speedup, bucket_score in sorted(buckets):
        if speedup >= min_speedup:
            score = bucket_score
    return score


def _write_inside(workdir: Path, rel: str, content: str) -> None:
    dest = (workdir / rel).resolve()
    dest.relative_to(workdir.resolve())  # task-authored, but never escape the workdir
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)


@register("speedup")
def verify(workdir: Path, args: dict) -> VerifyResult:
    python = _venv_python(args)
    if not python.exists():
        return VerifyResult(
            False,
            f"[verifier setup] venv python not found at {python}; set MODEL_BENCH_VENV "
            "or install the monolith venv (see projects/model-bench/README.md)",
        )
    baseline = args["baseline"]
    _write_inside(workdir, baseline["path"], baseline["source"])
    _write_inside(workdir, HARNESS_NAME, args["harness"])

    res = run_sandboxed(
        [str(python), HARNESS_NAME, *[str(a) for a in args.get("harness_args", [])]],
        cwd=workdir,
        timeout_s=args.get("timeout_s", 600),
        extra_env={"PYTHONPATH": ".", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    lines = (res.stdout or "").strip().splitlines()
    try:
        result = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        detail = (res.stdout or "") + (res.stderr or "")
        return VerifyResult(
            False, "harness produced no result:\n" + detail[-2000:], 0.0
        )
    if not result.get("ok"):
        return VerifyResult(
            False, result.get("detail", "correctness check failed"), 0.0
        )

    speedup = float(result["speedup"])
    score = _bucket_score(speedup, args["buckets"])
    feedback = f"speedup {speedup:.1f}x -> score {score:.2f}"
    if result.get("detail"):
        feedback += f" ({result['detail']})"
    return VerifyResult(score >= args.get("pass_threshold", 1.0), feedback, score)
