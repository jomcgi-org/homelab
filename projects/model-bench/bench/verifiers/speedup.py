"""Bucketed speedup grading with opt-in isolated, authenticated paired-v1 samples.

Legacy tasks retain their {ok, speedup, detail} output contract. New tasks use
the trusted helper's benchmark API and never supply their own ratio or score.
"""

from __future__ import annotations

import ast
import inspect
import json
import math
import secrets
import statistics
import sys
import tempfile
from pathlib import Path

from bench.schema import PerformanceRecord, PerformanceSample
from bench.verifiers import VerifyResult, register, speedup_protocol
from bench.verifiers.sandbox import run_sandboxed

HARNESS_NAME = "_speedup_harness.py"
HELPER_NAME = "_speedup_protocol.py"
DEFAULT_IMPORTS = {
    "__future__",
    "math",
    "collections",
    "itertools",
    "functools",
    "bisect",
    "heapq",
}
SUPPORTED_IMPORTS = DEFAULT_IMPORTS | {
    "random",
    "statistics",
    "json",
    "re",
    "array",
    "decimal",
    "fractions",
    "operator",
}
# Task allowlists cannot opt into interpreter/process introspection.
BANNED_IMPORTS = {
    "sys",
    "os",
    "gc",
    "inspect",
    "ctypes",
    "importlib",
    "builtins",
    "time",
    "threading",
    "multiprocessing",
    "subprocess",
    "signal",
    "atexit",
    "io",
    "pathlib",
}
BANNED_NAMES = {
    "__import__",
    "eval",
    "exec",
    "compile",
    "globals",
    "locals",
    "vars",
    "open",
    "input",
    "setattr",
    "delattr",
    "breakpoint",
    "getattr",
    "attrgetter",
    "methodcaller",
    "type",
}


def _bucket_score(speedup: float, buckets: list) -> float:
    score = 0.0
    for min_speedup, bucket_score in sorted(buckets):
        if speedup >= min_speedup:
            score = round(float(bucket_score), 12)
    return score


def _inside(root: Path, rel: str) -> Path:
    path = Path(rel)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"unsafe relative path: {rel!r}")
    dest = (root / path).resolve()
    dest.relative_to(root.resolve())
    return dest


def _write_inside(workdir: Path, rel: str, content: str) -> None:
    dest = _inside(workdir, rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)


def _check_source(source: str, allowed_imports: set[str]) -> None:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if isinstance(node, ast.ImportFrom) and node.level:
                raise ValueError("relative imports are forbidden")
            for name in names:
                if name.split(".")[0] not in allowed_imports or any(
                    p.startswith("_") for p in name.split(".") if p != "__future__"
                ):
                    raise ValueError(f"import {name!r} is not allowed")
            for alias in node.names:
                if alias.name == "*" or (
                    isinstance(node, ast.ImportFrom) and alias.name.startswith("_")
                ):
                    raise ValueError("private or wildcard imports are forbidden")
        if isinstance(node, ast.Attribute) and (
            node.attr.startswith(("_", "f_", "gi_", "cr_", "ag_", "tb_"))
            or node.attr in BANNED_NAMES | BANNED_IMPORTS | {"modules", "func_globals"}
            or isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            raise ValueError(f"attribute {node.attr!r} is forbidden")
        identifiers = []
        if isinstance(node, ast.Name):
            identifiers = [node.id]
        elif isinstance(node, ast.arg):
            identifiers = [node.arg]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifiers = [node.name]
        elif isinstance(node, ast.alias):
            identifiers = [node.asname or node.name]
            if node.name.split(".")[-1] in BANNED_NAMES | BANNED_IMPORTS:
                raise ValueError(f"name {node.name!r} is forbidden")
        for name in identifiers:
            # __del__ may print at teardown; the helper exits without running it.
            if name in BANNED_NAMES or (name.startswith("__") and name != "__del__"):
                raise ValueError(f"name {name!r} is forbidden")


def _record(args: dict, correctness: bool | None, **values) -> PerformanceRecord:
    return PerformanceRecord(
        correctness=correctness,
        score=0.0,
        pass_threshold=round(float(args.get("pass_threshold", 1.0)), 12),
        pair_count=args.get("pairs", 7),
        fixture_version=args["fixture_version"],
        **values,
    )


def _failure(args: dict, detail: str, *, harness=False, timeout=False) -> VerifyResult:
    record = (
        _record(args, None if harness or timeout else False)
        if args.get("protocol") == "paired-v1"
        else None
    )
    if not harness:
        detail = detail.replace("harness error]", "candidate error]")
    return VerifyResult(
        False, ("[harness error] " if harness else "") + detail, 0.0, record
    )


def _grade_samples(result: dict, args: dict) -> VerifyResult:
    if result.get("status") == "harness_error":
        return _failure(args, result.get("detail", "harness failed"), harness=True)
    if result.get("status") == "graded_failure":
        return _failure(args, result.get("detail", "correctness check failed"))
    try:
        if result.get("status") != "ok":
            raise ValueError("unknown protocol status")
        warmup = [PerformanceSample.model_validate(s) for s in result["warmup"]]
        samples = [PerformanceSample.model_validate(s) for s in result["samples"]]
        if len(warmup) != 1 or len(samples) != args.get("pairs", 7):
            raise ValueError("incorrect warm-up or measured-pair count")
        for index, sample in enumerate(samples):
            expected = "baseline-first" if index % 2 == 0 else "candidate-first"
            if sample.order != expected:
                raise ValueError("incorrect alternating pair order")
        if warmup[0].order != "baseline-first":
            raise ValueError("incorrect warm-up order")
        ratios = [s.baseline_s / s.candidate_s for s in samples]
        if not all(math.isfinite(r) and r > 0 for r in ratios):
            raise ValueError("non-finite paired ratio")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return _failure(args, f"invalid samples: {exc}", harness=True)
    ratio = statistics.median(ratios)
    score = _bucket_score(ratio, args["buckets"])
    bucket = max((float(b) for b, _ in args["buckets"] if ratio >= b), default=None)
    record = _record(
        args,
        True,
        warmup=warmup,
        samples=samples,
        ratios=ratios,
        median_ratio=ratio,
        highest_bucket=bucket,
    )
    record.score = score
    return VerifyResult(
        score >= record.pass_threshold,
        f"speedup {ratio:.1f}x -> score {score:.2f} (frozen dataset {record.fixture_version})",
        score,
        record,
    )


def _legacy_editable(workdir: Path, harness: str, baseline_path: str) -> list[str]:
    """Infer only script paths/imported local modules for pre-allowlist tasks."""
    paths = set()
    for node in ast.walk(ast.parse(harness)):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.endswith(".py")
        ):
            paths.add(node.value)
        if isinstance(node, ast.Import):
            paths.update(alias.name.replace(".", "/") + ".py" for alias in node.names)
    return sorted(
        p
        for p in paths
        if p != baseline_path
        and Path(p).parts[0].removesuffix(".py") not in sys.stdlib_module_names
        and _inside(workdir, p).is_file()
    )


def _validate_config(args: dict) -> None:
    buckets = args["buckets"]
    if not buckets or len({b for b, _ in buckets}) != len(buckets):
        raise ValueError("buckets must be nonempty with unique boundaries")
    for boundary, score in buckets:
        if (
            not math.isfinite(boundary)
            or boundary <= 0
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise ValueError("invalid bucket boundary or score")
    threshold = args.get("pass_threshold", 1.0)
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("invalid pass_threshold")
    if not math.isfinite(args.get("timeout_s", 600)) or args.get("timeout_s", 600) <= 0:
        raise ValueError("timeout_s must be finite and positive")
    if args.get("protocol") not in (None, "paired-v1"):
        raise ValueError("unknown speedup protocol")
    if args.get("protocol") == "paired-v1":
        if (
            type(args.get("pairs", 7)) is not int
            or not 1 <= args.get("pairs", 7) <= 100
        ):
            raise ValueError("pairs must be an integer from 1 through 100")
        if (
            not isinstance(args.get("fixture_version"), str)
            or not args["fixture_version"]
        ):
            raise ValueError("fixture_version is required")
        if not isinstance(args.get("editable"), list) or not args["editable"]:
            raise ValueError("editable is required")
        allowed = set(args.get("allowed_imports", DEFAULT_IMPORTS))
        if not allowed <= SUPPORTED_IMPORTS or allowed & BANNED_IMPORTS:
            raise ValueError("allowed_imports must contain safe stdlib module names")
        if type(args.get("seed", 0)) is not int:
            raise ValueError("seed must be an integer")


@register("speedup")
def verify(workdir: Path, args: dict) -> VerifyResult:
    try:
        _validate_config(args)
    except (KeyError, TypeError, ValueError) as exc:
        return VerifyResult(
            False, f"[harness error] invalid speedup configuration: {exc}", 0.0
        )
    python = Path(args.get("python", sys.executable)).resolve()
    if not python.is_file():
        return _failure(args, f"python not found at {python}", harness=True)
    new_style = args.get("protocol") == "paired-v1"
    nonce = secrets.token_hex(32)
    try:
        baseline = args["baseline"]
        editable = args.get("editable") or _legacy_editable(
            workdir, args["harness"], baseline["path"]
        )
        with tempfile.TemporaryDirectory(prefix="bench-speedup-") as directory:
            grading = Path(directory)
            _inside(grading, baseline["path"])
            for rel in editable:
                dest = _inside(grading, rel)
                if (
                    rel in {baseline["path"], HARNESS_NAME, HELPER_NAME}
                    or Path(rel).parts[0].removesuffix(".py") in sys.stdlib_module_names
                ):
                    return _failure(
                        args, f"reserved editable path: {rel}", harness=True
                    )
                try:
                    source = _inside(workdir, rel).read_text()
                    if new_style:
                        if not rel.endswith(".py"):
                            raise ValueError("editable files must be Python source")
                        _check_source(
                            source, set(args.get("allowed_imports", DEFAULT_IMPORTS))
                        )
                except (OSError, UnicodeError, ValueError, SyntaxError) as exc:
                    return _failure(args, f"rejected candidate source {rel}: {exc}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(source)
            _write_inside(grading, baseline["path"], baseline["source"])
            _write_inside(grading, HARNESS_NAME, args["harness"])
            if new_style:
                _write_inside(grading, HELPER_NAME, inspect.getsource(speedup_protocol))
                bootstrap = (
                    "import importlib.util; "
                    f"s=importlib.util.spec_from_file_location('trusted_speedup', {str(grading / HELPER_NAME)!r}); "
                    "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                    f"m.run_harness({HARNESS_NAME!r}, {args.get('pairs', 7)!r}, {args.get('seed', 0)!r})"
                )
            else:
                bootstrap = f"import runpy,sys; sys.path.insert(0, {str(grading)!r}); runpy.run_path({HARNESS_NAME!r}, run_name='__main__')"
            res = run_sandboxed(
                [
                    str(python),
                    "-I",
                    "-B",
                    "-S",
                    "-c",
                    bootstrap,
                    *map(str, args.get("harness_args", [])),
                ],
                cwd=grading,
                timeout_s=args.get("timeout_s", 600),
                input_text=nonce + "\n",
            )
    except (OSError, ValueError, KeyError, SyntaxError) as exc:
        return _failure(args, f"setup failed: {exc}", harness=True)
    if res.timed_out:
        return _failure(args, "candidate exceeded timeout_s", timeout=True)
    lines = (res.stdout or "").strip().splitlines()
    if new_style:
        authenticated = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("nonce") == nonce:
                authenticated.append(value)
        if res.rc != 0 or len(authenticated) != 1:
            return _failure(
                args,
                "harness produced no result or ambiguous nonce-bearing result",
                harness=True,
            )
        return _grade_samples(authenticated[0], args)
    try:
        result = json.loads(lines[-1])
        if not result.get("ok"):
            return _failure(args, result.get("detail", "correctness check failed"))
        ratio = float(result["speedup"])
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError("invalid legacy ratio")
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        return _failure(args, f"harness produced no result: {exc}", harness=True)
    score = _bucket_score(ratio, args["buckets"])
    return VerifyResult(
        score >= round(args.get("pass_threshold", 1.0), 12),
        f"speedup {ratio:.1f}x -> score {score:.2f} ({result.get('detail', '')})",
        score,
    )


verify.source_dependencies = (speedup_protocol,)
