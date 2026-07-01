from pathlib import Path

from bench.sandbox import run_sandboxed
from bench.verifiers import register, VerifyResult


@register("ruff")
def verify_ruff(workdir: Path, args: dict) -> VerifyResult:
    """Run ruff lint check on a path (default: current dir)."""
    res = run_sandboxed(
        ["ruff", "check", args.get("path", ".")],
        cwd=workdir,
        timeout_s=60,
    )
    if res.rc == 0:
        return VerifyResult(True, "")
    return VerifyResult(False, res.stderr or res.stdout or f"exit {res.rc}")


@register("buildifier")
def verify_buildifier(workdir: Path, args: dict) -> VerifyResult:
    """Run buildifier format-check on a BUILD file."""
    res = run_sandboxed(
        ["buildifier", "--mode=check", args["file"]],
        cwd=workdir,
        timeout_s=30,
    )
    if res.rc == 0:
        return VerifyResult(True, "")
    return VerifyResult(False, res.stderr or res.stdout or f"exit {res.rc}")
