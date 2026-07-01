from pathlib import Path

from bench.verifiers.sandbox import run_sandboxed
from bench.verifiers import register, VerifyResult


@register("helm-template")
def verify(workdir: Path, args: dict) -> VerifyResult:
    """Run helm template and assert on rendered YAML content (effect, not just exit code)."""
    release = args["release"]
    chart = args["chart"]
    values = args.get("values")
    assert_contains = args.get("assert_contains", [])
    refute_contains = args.get("refute_contains", [])

    cmd = ["helm", "template", release, chart]
    if values:
        cmd += ["-f", values]

    res = run_sandboxed(cmd, cwd=workdir, timeout_s=120)
    if res.rc != 0:
        return VerifyResult(False, res.stderr or res.stdout)

    render = res.stdout

    for s in assert_contains:
        if s not in render:
            return VerifyResult(
                False,
                f"expected to find {s!r} in rendered output but did not.\n--- rendered ---\n{render}",
            )

    for s in refute_contains:
        if s in render:
            return VerifyResult(
                False,
                f"forbidden string {s!r} found in rendered output.\n--- rendered ---\n{render}",
            )

    return VerifyResult(True, "")
