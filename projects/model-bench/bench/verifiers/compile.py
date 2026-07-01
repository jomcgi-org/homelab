from pathlib import Path

from bench.sandbox import run_sandboxed
from bench.verifiers import register, VerifyResult


@register("py-compile")
def verify_py(workdir: Path, args: dict) -> VerifyResult:
    """Verify a Python file compiles without syntax errors."""
    res = run_sandboxed(
        [
            "python",
            "-c",
            "import py_compile,sys; py_compile.compile(sys.argv[1], doraise=True)",
            args["file"],
        ],
        cwd=workdir,
        timeout_s=30,
    )
    if res.rc == 0:
        return VerifyResult(True, "")
    return VerifyResult(False, res.stderr or res.stdout or f"exit {res.rc}")


@register("go-build")
def verify_go(workdir: Path, args: dict) -> VerifyResult:
    """Verify a Go package builds cleanly."""
    res = run_sandboxed(["go", "build", "./..."], cwd=workdir, timeout_s=120)
    if res.rc == 0:
        return VerifyResult(True, "")
    return VerifyResult(False, res.stderr or res.stdout or f"exit {res.rc}")
