"""Real-test verifier: grade an agentic edit with the repo's own pytest suite.

The SWE-bench contract for this benchmark: the fixture is the monolith source at the
PARENT of a real fix commit (buggy state), and the grader is the gold test from the fix
commit. We write the gold test file(s) onto the workdir on top of whatever the model
produced, then run them on the vendored monolith venv with PYTHONPATH pointing at the
fixture root so `from home.observability.slo import ...` resolves against the snapshot.

Fail-to-pass: on the unmodified (buggy) snapshot the gold test fails; a correct model
edit makes it pass. The test text lives in the task's verifier args (never in the
fixture the model explores), so it stays a hidden grader.
"""

from __future__ import annotations

import os
from pathlib import Path

from bench.verifiers import VerifyResult, register
from bench.verifiers.sandbox import run_sandboxed

DEFAULT_VENV = Path.home() / ".cache" / "model-bench-venv"


def _venv_python(args: dict) -> Path:
    """Resolve the interpreter that has the monolith's runtime deps installed.

    Precedence: explicit args["python"] > $MODEL_BENCH_VENV/bin/python > the default
    ~/.cache/model-bench-venv. Kept out of task.yaml so fixtures stay machine-portable.
    """
    explicit = args.get("python")
    if explicit:
        return Path(explicit)
    root = os.environ.get("MODEL_BENCH_VENV")
    base = Path(root) if root else DEFAULT_VENV
    return base / "bin" / "python"


@register("pytest")
def verify(workdir: Path, args: dict) -> VerifyResult:
    python = _venv_python(args)
    if not python.exists():
        return VerifyResult(
            False,
            f"[verifier setup] venv python not found at {python}; set MODEL_BENCH_VENV "
            "or install the monolith venv (see projects/model-bench/README.md)",
        )

    # Drop the gold test file(s) on top of the model's workdir (hidden grader).
    for name, content in args.get("tests", {}).items():
        dest = workdir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)

    targets = args.get("targets") or list(args.get("tests", {}).keys())
    if not targets:
        return VerifyResult(False, "[verifier setup] pytest verifier needs targets")

    cmd = [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets]
    res = run_sandboxed(
        cmd,
        cwd=workdir,
        timeout_s=args.get("timeout_s", 180),
        # PYTHONPATH=. makes the fixture root a package root so the gold test imports
        # the model's edited modules (home.*, knowledge.*, ...) from the snapshot.
        extra_env={"PYTHONPATH": "."},
    )
    if res.rc == 0:
        return VerifyResult(True, "")
    # Surface the tail of the pytest output so a failure is diagnosable in the cell.
    detail = (res.stdout or "") + (res.stderr or "")
    return VerifyResult(False, detail[-2000:] or f"exit {res.rc}")
