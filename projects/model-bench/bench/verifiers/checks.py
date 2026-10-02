"""Graded-checks verifier: a task-authored script scores the workdir check by check.

The task's hidden `script` (python source in task.yaml, never in the fixture) runs on
the verifier venv with the workdir as cwd and prints one JSON object as its last stdout
line: {"checks": {"<name>": true | false | 0..1, ...}}. The score is the weighted mean
of the checks (`weights` maps a name to its weight, default 1 each), so a model that
gets the change half right lands at 0.5 rather than at "fail".

Tasks that render Helm resolve the binary from args["helm"] > $MODEL_BENCH_HELM >
`helm` on PATH and receive it as $HELM, so the script never hard-codes a machine path.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from bench.verifiers import VerifyResult, register
from bench.verifiers.pytest import _venv_python
from bench.verifiers.sandbox import run_sandboxed


def _helm(args: dict) -> str:
    return (
        args.get("helm")
        or os.environ.get("MODEL_BENCH_HELM")
        or shutil.which("helm")
        or "helm"
    )


def _score(checks: dict, weights: dict) -> float:
    total = sum(weights.get(name, 1.0) for name in checks)
    if not total:
        return 0.0
    got = sum(weights.get(name, 1.0) * float(value) for name, value in checks.items())
    return got / total


@register("checks")
def verify(workdir: Path, args: dict) -> VerifyResult:
    python = _venv_python(args)
    if not python.exists():
        return VerifyResult(
            False,
            f"[verifier setup] venv python not found at {python}; set MODEL_BENCH_VENV "
            "or install the monolith venv (see projects/model-bench/README.md)",
        )
    # The script lives outside the workdir so the model's files cannot shadow it.
    script_dir = Path(tempfile.mkdtemp())
    try:
        script = script_dir / "check.py"
        script.write_text(args["script"])
        res = run_sandboxed(
            [str(python), str(script)],
            cwd=workdir,
            timeout_s=args.get("timeout_s", 120),
            extra_env={"HELM": _helm(args)},
        )
    finally:
        shutil.rmtree(script_dir, ignore_errors=True)

    lines = (res.stdout or "").strip().splitlines()
    try:
        checks = json.loads(lines[-1])["checks"]
    except (IndexError, ValueError, KeyError, TypeError):
        detail = (res.stdout or "") + (res.stderr or "")
        return VerifyResult(
            False,
            f"[check script] no checks JSON (exit {res.rc}):\n{detail[-2000:]}",
            0.0,
        )
    expected = args.get("weights", {})
    # A check the task weights but the script never reported counts as failed, so a
    # script that crashes halfway cannot inflate the score by omission.
    for name in expected:
        checks.setdefault(name, False)
    score = _score(checks, expected)
    failed = [name for name, value in checks.items() if float(value) < 1.0]
    feedback = f"{len(checks) - len(failed)}/{len(checks)} checks passed"
    if failed:
        feedback += f"; failed: {', '.join(failed)}"
    return VerifyResult(score >= args.get("pass_threshold", 1.0), feedback, score)
