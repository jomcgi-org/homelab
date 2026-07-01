from pathlib import Path

from bench.verifiers.sandbox import run_sandboxed
from bench.verifiers import register, VerifyResult


@register("command")
def verify(workdir: Path, args: dict) -> VerifyResult:
    # Optionally drop hidden grading files (e.g. a Go/Python test the model never
    # sees) into the workdir before running the command, so behavioral tasks can
    # assert on real execution without leaking the test into the fixture.
    for name, content in args.get("write_files", {}).items():
        dest = workdir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    res = run_sandboxed(args["cmd"], cwd=workdir, timeout_s=args.get("timeout_s", 120))
    if res.rc == 0:
        return VerifyResult(True, "")
    return VerifyResult(False, res.stderr or res.stdout or f"exit {res.rc}")
