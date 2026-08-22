"""Command-line interface for the qwen lane probe."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from probe.fixture import (
    MissingCommitError,
    apply_guest_diff,
    ensure_snapshot_commit,
    is_in_scope,
    materialize_fixture,
)
from probe.report import load_jsonl, render_report, token_total
from probe.spans import HOPS, bucket_spans, collect_spans

SETS = {
    "fast": ["commit-message-01", "slo-budget-breach-01", "null-content-fix-01"],
    "long": [
        "go-vsock-frame-01",
        "worldcup-swing-settled-01",
        "research-adr-writeback-01",
    ],
}


@dataclass
class LoadedTask:
    spec: Any
    mapping: dict

    @property
    def snapshot(self) -> dict | None:
        from bench.cli import _resolve_snapshot_preset

        value = self.mapping.get("snapshot")
        if not isinstance(value, dict):
            return None
        return _resolve_snapshot_preset(value)


def _model_bench_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_repo_path() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("could not find the homelab checkout above the probe package")


def _load_tasks(tasks_dir: Path) -> dict[str, LoadedTask]:
    from bench.cli import _load_yaml_mapping
    from bench.schema import TaskSpec

    tasks = {}
    for task_file in sorted(tasks_dir.glob("*/task.yaml")):
        mapping = _load_yaml_mapping(task_file)
        spec = TaskSpec.model_validate(mapping)
        tasks[spec.id] = LoadedTask(spec, mapping)
    return tasks


def _prompt(task: LoadedTask) -> str:
    snapshot = task.snapshot
    if snapshot is None:
        return task.spec.prompt
    paths = ", ".join(str(path) for path in snapshot.get("paths", []))
    prefix = (
        "The checkout is at /workspace/src. "
        f"Work only under {paths} (paths relative to the repo root). "
        "Do not commit or push; leave your changes in the working tree."
    )
    return f"{prefix}\n\n{task.spec.prompt}"


def _field(turn: dict, name: str):
    value = turn.get(name)
    if value is not None:
        return value
    usage = turn.get("usage")
    return usage.get(name) if isinstance(usage, dict) else None


def _output_tokens(usage: dict) -> int | float | None:
    for key in ("output_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return value
    return None


def _approx_tok_s(turn: dict) -> float | None:
    usage = turn.get("usage") if isinstance(turn.get("usage"), dict) else {}
    tokens = _output_tokens(usage)
    duration_ms = _field(turn, "duration_ms")
    if not isinstance(tokens, (int, float)) or not isinstance(
        duration_ms, (int, float)
    ):
        return None
    if duration_ms <= 0:
        return None
    return round(float(tokens) / (float(duration_ms) / 1000), 3)


def _extra_apply_files(task: LoadedTask) -> list[str]:
    if task.spec.verifier.kind != "json-match":
        return []
    verifier_file = task.spec.verifier.args.get("file")
    return [verifier_file] if isinstance(verifier_file, str) else []


def _grade(
    task: LoadedTask, fixture_dir: Path, result_text: str
) -> tuple[bool, str, str]:
    if task.spec.verifier.kind == "judge":
        if not os.environ.get("OPENROUTER_API_KEY"):
            return False, "ungraded (no judge key)", "OPENROUTER_API_KEY is unset"
        from bench.claude_code import judge_caller
        from bench.judge import JudgeConfig, judge_free_text
        from bench.runner import _strip_code_fence

        config = JudgeConfig(
            judge_model=task.spec.verifier.args["judge_model"],
            criteria=task.spec.verifier.args["criteria"],
            permutations=task.spec.verifier.args.get("permutations", 2),
        )
        judged = judge_free_text(
            candidate=_strip_code_fence(result_text),
            task_prompt=task.spec.prompt,
            cfg=config,
            caller=judge_caller,
            candidate_model="qwen",
        )
        return judged.passed, "pass" if judged.passed else "fail", str(judged.votes)

    from bench.verifiers import get_verifier

    verifier = get_verifier(task.spec.verifier.kind)
    verified = verifier(fixture_dir, task.spec.verifier.args)
    return verified.passed, "pass" if verified.passed else "fail", verified.feedback


def _blank_result(task_id: str, rep: int, started_at: datetime) -> dict:
    return {
        "task": task_id,
        "rep": rep,
        "session_id": None,
        "turn": None,
        "passed": False,
        "grade": "not_run",
        "feedback": "",
        "wall_s": 0.0,
        "accept_s": 0.0,
        "duration_ms": None,
        "num_turns": None,
        "usage": {},
        "diff_files": [],
        "diff_truncated": False,
        "out_of_scope_files": [],
        "span_buckets": {},
        "started_at": started_at.isoformat(),
        "ended_at": started_at.isoformat(),
        "gpu_note": "",
        "approx_tok_s": None,
    }


def _run_once(task: LoadedTask, rep: int, args) -> dict:
    from probe.session import AgentSessionClient, poll_turn, unified_diff

    snapshot = task.snapshot
    if snapshot is not None:
        ensure_snapshot_commit(Path(args.repo_path), snapshot)
    # The HTTP router treats a null repo as "no checkout"; an empty string is
    # rejected as an unknown catalog entry.
    repo = args.repo if snapshot is not None else None
    branch = args.branch or f"bench/{task.spec.id}"
    payload = {
        "prompt": _prompt(task),
        "model": "qwen",
        "repo": repo,
        "branch": branch,
    }
    client = AgentSessionClient(args.base_url, timeout_s=min(30, args.timeout))
    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    result = _blank_result(task.spec.id, rep, started_at)
    session_id = None
    terminal_at = started_at
    try:
        accept_start = time.monotonic()
        response = client.start(payload)
        result["accept_s"] = round(time.monotonic() - accept_start, 3)
        session_id = response.get("session_id")
        result["session_id"] = session_id
        result["turn"] = response.get("turn")
        if not response.get("accepted"):
            terminal_at = datetime.now(timezone.utc)
            result["grade"] = "start_failed"
            result["feedback"] = str(response.get("error", "session was not accepted"))
            result["wall_s"] = round(time.monotonic() - started_monotonic, 3)
            return result
        if not isinstance(session_id, int) or not isinstance(response.get("turn"), int):
            raise TypeError("accepted response lacks integer session_id or turn")

        outcome = poll_turn(
            client,
            session_id,
            response["turn"],
            timeout_s=args.timeout,
            started_monotonic=started_monotonic,
        )
        terminal_at = datetime.now(timezone.utc)
        result["wall_s"] = round(outcome.wall_s, 3)
        if outcome.timed_out or outcome.turn is None:
            result["grade"] = "timeout"
            result["feedback"] = f"turn did not complete within {args.timeout:g}s"
            return result

        turn = outcome.turn
        usage = turn.get("usage") if isinstance(turn.get("usage"), dict) else {}
        result["usage"] = usage
        result["duration_ms"] = _field(turn, "duration_ms")
        result["num_turns"] = _field(turn, "num_turns")
        result["approx_tok_s"] = _approx_tok_s(turn)
        result["terminal_reason"] = _field(turn, "terminal_reason")
        if turn.get("terminal_reason") == "error":
            # The guest never produced a diff. Grading the GitHub compare
            # fallback here would score the whole branch history.
            result["grade"] = "error"
            result["feedback"] = str(turn.get("result_text") or "")[:2000]
            return result

        compare = client.compare(session_id, response["turn"])
        if task.snapshot is not None and compare.get("diff_type") != "stored":
            # Only the diff captured inside the guest is the agent's work. The
            # compare router falls back to a GitHub base..head compare when no
            # blob is stored, which is not what the agent did.
            result["grade"] = "no_stored_diff"
            result["feedback"] = (
                f"compare diff_type={compare.get('diff_type')!r} "
                f"resolution_rung={compare.get('resolution_rung')!r}"
            )
            return result
        files = compare.get("files", [])
        files = (
            [item for item in files if isinstance(item, dict)]
            if isinstance(files, list)
            else []
        )
        result["diff_files"] = [
            str(item.get("path")) for item in files if item.get("path")
        ]
        stats = compare.get("stats") if isinstance(compare.get("stats"), dict) else {}
        result["diff_truncated"] = bool(
            turn.get("diff_truncated")
            or compare.get("diff_truncated")
            or compare.get("truncated")
            or stats.get("truncated_at")
        )
        patches = {}
        for item in files:
            path = item.get("path")
            if isinstance(path, str):
                patch_payload = client.patch(session_id, response["turn"], path)
                patch = patch_payload.get("patch")
                patches[path] = patch if isinstance(patch, str) else None
        diff = unified_diff(files, patches)

        if snapshot is None:
            fixture_root = Path(tempfile.mkdtemp(prefix="qwen-probe-empty-"))
            try:
                passed, grade, feedback = _grade(
                    task, fixture_root, str(turn.get("result_text", ""))
                )
            finally:
                fixture_root.rmdir()
        else:
            with tempfile.TemporaryDirectory(prefix="qwen-probe-") as temp_dir:
                fixture_root = Path(temp_dir)
                materialize_fixture(Path(args.repo_path), snapshot, fixture_root)
                apply_result = apply_guest_diff(
                    fixture_root,
                    diff,
                    snapshot,
                    extra_files=_extra_apply_files(task),
                    changed_files=result["diff_files"],
                )
                result["out_of_scope_files"] = apply_result.out_of_scope_files
                missing_patches = [
                    path
                    for path, patch in patches.items()
                    if patch is None
                    and is_in_scope(path, snapshot, _extra_apply_files(task))
                ]
                if missing_patches:
                    passed, grade, feedback = (
                        False,
                        "apply_failed",
                        "compare returned no patch for: " + ", ".join(missing_patches),
                    )
                elif not apply_result.applied:
                    passed, grade, feedback = (
                        False,
                        "apply_failed",
                        apply_result.error,
                    )
                else:
                    try:
                        passed, grade, feedback = _grade(
                            task, fixture_root, str(turn.get("result_text", ""))
                        )
                    except Exception as exc:  # noqa: BLE001
                        passed, grade, feedback = False, "verifier_failed", str(exc)
        result["passed"] = passed
        result["grade"] = grade
        result["feedback"] = feedback[:2000]
        return result
    except Exception as exc:  # noqa: BLE001
        terminal_at = datetime.now(timezone.utc)
        result["grade"] = "probe_error"
        result["feedback"] = str(exc)[:2000]
        result["wall_s"] = round(time.monotonic() - started_monotonic, 3)
        return result
    finally:
        result["ended_at"] = terminal_at.isoformat()
        if not args.no_spans:
            try:
                result["span_buckets"] = collect_spans(started_at, terminal_at)
            except Exception as exc:  # noqa: BLE001
                print(f"warning: span query failed: {exc}", file=sys.stderr)
                result["span_buckets"] = bucket_spans([])
        if isinstance(session_id, int) and not args.keep_session:
            try:
                client.delete(session_id)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"warning: session cleanup failed for {session_id}: {exc}",
                    file=sys.stderr,
                )
        client.close()


def _print_result(result: dict) -> None:
    print(
        f"{result['task']} rep {result['rep']}: grade={result['grade']} "
        f"passed={result['passed']} wall_s={result['wall_s']} "
        f"accept_s={result['accept_s']} turns={result['num_turns']} "
        f"tokens={token_total(result) if token_total(result) is not None else 'n/a'} "
        f"approx_tok_s={result['approx_tok_s'] if result['approx_tok_s'] is not None else 'n/a'}"
    )
    span_data = result.get("span_buckets") or bucket_spans([])
    buckets = span_data.get("buckets", span_data)
    seen = span_data.get("service_names_seen", [])
    print("hop                         spans   total_ms   max_ms")
    for hop in HOPS:
        bucket = buckets.get(hop, {})
        print(
            f"{hop:<28} {bucket.get('spans', 0):>5} "
            f"{bucket.get('total_ms', 0):>10.1f} {bucket.get('max_ms', 0):>8.1f}"
        )
    phases = span_data.get("embervm_phases", {})
    for name, value in sorted(phases.items()):
        print(f"{name}: {value:.1f}ms")
    guest_services = buckets.get("guest / shim", {}).get("service_names", [])
    if guest_services:
        print(f"guest / shim services: {', '.join(guest_services)}")
    if any(not bucket.get("spans", 0) for bucket in buckets.values()):
        print(f"serviceNames seen: {', '.join(seen) if seen else 'none'}")


def _run(args) -> None:
    tasks = _load_tasks(Path(args.tasks))
    requested = (
        SETS[args.set_name]
        if args.set_name
        else [item.strip() for item in args.task.split(",") if item.strip()]
    )
    missing = [task_id for task_id in requested if task_id not in tasks]
    if missing:
        raise SystemExit(f"unknown task(s): {', '.join(missing)}")
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a") as stream:
        for task_id in requested:
            for rep in range(1, args.reps + 1):
                try:
                    result = _run_once(tasks[task_id], rep, args)
                except MissingCommitError as exc:
                    raise SystemExit(str(exc)) from exc
                stream.write(json.dumps(result, sort_keys=True) + "\n")
                stream.flush()
                _print_result(result)


def _sets(_args) -> None:
    for name, tasks in SETS.items():
        print(f"{name}: {','.join(tasks)}")


def _report(args) -> None:
    print(render_report(load_jsonl(Path(args.input))), end="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="probe")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run tasks through the qwen session lane")
    selection = run.add_mutually_exclusive_group(required=True)
    selection.add_argument("--task", help="comma-separated task ids")
    selection.add_argument("--set", dest="set_name", choices=sorted(SETS))
    run.add_argument("--reps", type=int, default=1)
    run.add_argument("--base-url", default="http://127.0.0.1:18000")
    run.add_argument("--branch")
    run.add_argument("--repo", default="jomcgi/homelab")
    run.add_argument("--repo-path", default=str(_default_repo_path()))
    run.add_argument("--timeout", type=float, default=4200)
    run.add_argument("--keep-session", action="store_true")
    run.add_argument("--no-spans", action="store_true")
    run.add_argument("--out", default="results.jsonl")
    run.add_argument(
        "--tasks", default=str(_model_bench_root() / "tasks"), help=argparse.SUPPRESS
    )
    run.set_defaults(func=_run)

    report = subparsers.add_parser("report", help="render medians from probe JSONL")
    report.add_argument("--in", dest="input", required=True)
    report.set_defaults(func=_report)

    sets = subparsers.add_parser("sets", help="print named task sets")
    sets.set_defaults(func=_sets)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if getattr(args, "reps", 1) < 1:
        raise SystemExit("--reps must be at least 1")
    if getattr(args, "timeout", 1) <= 0:
        raise SystemExit("--timeout must be greater than 0")
    args.func(args)
