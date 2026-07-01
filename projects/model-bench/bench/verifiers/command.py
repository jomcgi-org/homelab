from pathlib import Path

from bench.sandbox import run_sandboxed
from bench.verifiers import register, VerifyResult


@register("command")
def verify(workdir: Path, args: dict) -> VerifyResult:
    res = run_sandboxed(args["cmd"], cwd=workdir, timeout_s=args.get("timeout_s", 120))
    if res.rc == 0:
        return VerifyResult(True, "")
    return VerifyResult(False, res.stderr or res.stdout or f"exit {res.rc}")
