"""Mutation-testing verifier: grade a model-written test suite by the bugs it catches.

The model writes tests for a real module; it never sees this grader. The suite must
pass on the unmodified module, then each hidden mutant (a single find/replace on the
module, modelled on a plausible real bug) is applied to a fresh copy of the workdir and
the suite is re-run. A mutant is killed when the suite fails on it. The score is the
fraction killed, so a happy-path suite and a boundary-hunting one land at different
points rather than both "passing".

A suite with some wrong tests is not thrown away: tests that fail on the unmodified
module are dropped, the rest are run against the mutants, and the kill fraction is
scaled by the share of tests that were correct. A suite where nothing passes scores 0.

Equivalent mutants are behaviour-preserving rewrites (reordered conditions, an edited
comment). A behavioural test cannot tell them apart from the original, so killing one
means the suite asserts on source text (a hash, inspect.getsource) instead of
behaviour: that zeroes the score. The module's sha256 is pinned for the same reason,
so a model cannot "pass" by editing the module under test.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bench.verifiers import VerifyResult, register
from bench.verifiers.pytest import _venv_python
from bench.verifiers.sandbox import SandboxResult, run_sandboxed

MAX_WORKERS = 8


def _apply(source: str, mutant: dict) -> str:
    """Apply one find/replace mutant. The find text must occur exactly once, so a
    fixture bump that drifts the module fails loudly instead of grading a no-op."""
    n = source.count(mutant["find"])
    if n != 1:
        raise ValueError(
            f"mutant {mutant['id']!r}: find text occurs {n} times in the module (want 1)"
        )
    return source.replace(mutant["find"], mutant["replace"])


def _run_suite(
    workdir: Path,
    python: Path,
    tests: list[str],
    timeout_s: int,
    flags: tuple[str, ...] = ("-x",),
) -> SandboxResult:
    # -x: one failure is enough to call a mutant killed. A timeout (rc 124) counts as
    # a kill too: an infinite loop is a caught bug.
    cmd = [str(python), "-m", "pytest", "-q", *flags, "-p", "no:cacheprovider", *tests]
    return run_sandboxed(
        cmd,
        cwd=workdir,
        timeout_s=timeout_s,
        # No bytecode: a same-size mutant written within the same second as a cached
        # .pyc would otherwise be shadowed by the stale original.
        extra_env={"PYTHONPATH": ".", "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _killed(
    workdir: Path,
    module: str,
    mutated: str,
    python: Path,
    tests: list[str],
    timeout_s: int,
) -> bool:
    tmp = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(
            workdir,
            tmp,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
        )
        (tmp / module).write_text(mutated)
        return _run_suite(tmp, python, tests, timeout_s).rc != 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@register("mutation")
def verify(workdir: Path, args: dict) -> VerifyResult:
    python = _venv_python(args)
    if not python.exists():
        return VerifyResult(
            False,
            f"[verifier setup] venv python not found at {python}; set MODEL_BENCH_VENV "
            "or install the monolith venv (see projects/model-bench/README.md)",
        )
    module: str = args["module"]
    tests: list[str] = args["tests"]
    mutants: list[dict] = args["mutants"]
    equivalent: list[dict] = args.get("equivalent", [])
    threshold: float = args.get("pass_threshold", 1.0)
    timeout_s: int = args.get("timeout_s", 60)

    missing = [t for t in tests if not (workdir / t).is_file()]
    if missing:
        return VerifyResult(False, f"no test file at {', '.join(missing)}", 0.0)

    source = (workdir / module).read_text()
    if hashlib.sha256(source.encode()).hexdigest() != args["module_sha256"]:
        return VerifyResult(
            False, f"{module} was modified; only the tests may change", 0.0
        )

    # -rA lists every outcome by node id. Passing ids carry no trailing message, so
    # they parse cleanly and become the exact selection run against each mutant.
    baseline = _run_suite(workdir, python, tests, timeout_s, flags=("-rA",))
    lines = (baseline.stdout or "").splitlines()
    passing = [ln.split(" ", 1)[1] for ln in lines if ln.startswith("PASSED ")]
    n_bad = sum(ln.startswith(("FAILED ", "ERROR ")) for ln in lines)
    if not passing or baseline.timed_out:
        detail = (baseline.stdout or "") + (baseline.stderr or "")
        return VerifyResult(
            False,
            "no test passes on the unmodified module:\n" + detail[-2000:],
            0.0,
        )
    correct = len(passing) / (len(passing) + n_bad)
    selection = tests if n_bad == 0 else passing

    # Resolve every patch up front so a stale task fails before any suite runs.
    jobs = [(m["id"], False, _apply(source, m)) for m in mutants] + [
        (m["id"], True, _apply(source, m)) for m in equivalent
    ]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        kills = list(
            pool.map(
                lambda job: _killed(
                    workdir, module, job[2], python, selection, timeout_s
                ),
                jobs,
            )
        )

    false_kills = [mid for (mid, equiv, _), k in zip(jobs, kills) if equiv and k]
    if false_kills:
        return VerifyResult(
            False,
            "tests fail on behaviour-preserving rewrites of the module "
            f"({', '.join(false_kills)}); assert on behaviour, not source text",
            0.0,
        )

    real = [(mid, k) for (mid, equiv, _), k in zip(jobs, kills) if not equiv]
    killed = sum(k for _, k in real)
    score = killed / len(real) * correct
    survived = [mid for mid, k in real if not k]
    feedback = f"killed {killed}/{len(real)} mutants"
    if n_bad:
        feedback += (
            f"; {n_bad} of {len(passing) + n_bad} tests failed on the unmodified "
            f"module and were dropped (score x{correct:.2f})"
        )
    if survived:
        feedback += f"; survived: {', '.join(survived)}"
    return VerifyResult(score >= threshold, feedback, score)
