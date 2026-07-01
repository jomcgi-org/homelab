"""CLI entry-point for model-bench.

Subcommands:
  run     - Execute benchmark cells for all active (task, model) pairs.
  report  - Generate the leaderboard markdown report from cached results.
  drop    - Retire a model in models.yaml.
  prune   - Delete result files for retired models.
  list    - Print all models and their status/role.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

import yaml

from bench.agent import run_agent_cell
from bench.cache import (
    HARNESS_VERSION,
    cell_key,
    cell_path,
    fixture_hash,
    is_cached,
)
from bench.judge import JudgeConfig, judge_free_text
from bench.openrouter import OpenRouterClient
from bench.pareto import aggregate_by_class, coarse_tier, pareto_frontier, qualifies
from bench.registry import (
    active_models,
    anchors,
    drop_model,
    load_registry,
    prune_retired,
)
from bench.report import render_leaderboard
from bench.runner import _strip_code_fence, run_cell
from bench.schema import Attempt, ResultCell, TaskSpec
from bench.verifiers import get_verifier, verifier_source_hash


def _load_yaml_mapping(p: Path) -> dict:
    """Load YAML from path and verify it is a top-level mapping.

    The isinstance check is in this helper so the yaml.safe_load result is
    never subscripted in the same scope without a prior type guard, satisfying
    the repo semgrep rule yaml-safe-load-unchecked-type.
    """
    data = yaml.safe_load(p.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {p}, got {type(data).__name__}")
    return data


def load_tasks(tasks_dir: Path) -> list[TaskSpec]:
    """Load all task.yaml files found in immediate subdirectories of tasks_dir.

    Each subdirectory that contains a task.yaml is treated as a task pack.
    Results are sorted by task id for deterministic ordering.
    """
    tasks: list[TaskSpec] = []
    for subdir in tasks_dir.iterdir():
        if not subdir.is_dir():
            continue
        task_file = subdir / "task.yaml"
        if not task_file.exists():
            continue
        mapping = _load_yaml_mapping(task_file)
        tasks.append(TaskSpec.model_validate(mapping))
    return sorted(tasks, key=lambda t: t.id)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="model-bench",
        description="Model benchmarking harness for homelab AI workloads",
    )
    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Run benchmark cells")
    p_run.add_argument("--tasks", default="tasks", help="Path to tasks directory")
    p_run.add_argument("--models", default="models.yaml", help="Path to models.yaml")
    p_run.add_argument(
        "--results", default="results", help="Directory to store result JSON files"
    )
    p_run.add_argument(
        "--reports", default="reports", help="Directory for report output"
    )
    p_run.add_argument(
        "--force", action="store_true", help="Re-run even if a cached result exists"
    )
    p_run.add_argument(
        "--include-experimental",
        action="store_true",
        help="Include models with status=experimental",
    )
    p_run.add_argument(
        "--concurrency", type=int, default=6, help="Maximum concurrent API calls"
    )
    p_run.add_argument(
        "--task",
        default=None,
        help="Only run tasks whose id is in this comma-separated list (default: all)",
    )
    p_run.add_argument(
        "--model",
        dest="model_filter",
        default=None,
        help="Only run models whose id contains this substring (default: all)",
    )

    # report
    p_report = sub.add_parser("report", help="Generate leaderboard report")
    p_report.add_argument("--results", default="results")
    p_report.add_argument("--models", default="models.yaml")
    p_report.add_argument("--tasks", default="tasks")
    p_report.add_argument("--out", default="reports/leaderboard.md")
    p_report.add_argument(
        "--json-out",
        default=None,
        help="Also write a structured leaderboard JSON here (for the public page)",
    )
    p_report.add_argument(
        "--generated-at",
        default=None,
        help="Date stamp to embed in the JSON (default: today)",
    )

    # drop
    p_drop = sub.add_parser("drop", help="Retire a model in the registry")
    p_drop.add_argument(
        "model", help="Model id to retire (e.g. qwen/qwen-2.5-coder-32b-instruct)"
    )
    p_drop.add_argument("--reason", required=True, help="Short retirement reason")

    # prune
    p_prune = sub.add_parser("prune", help="Delete result files for retired models")
    p_prune.add_argument(
        "--retired", action="store_true", help="(reserved, currently unused)"
    )
    p_prune.add_argument(
        "--results", default="results", help="Directory to read result JSON files from"
    )

    # prune-stale
    p_prune_stale = sub.add_parser(
        "prune-stale",
        help="Delete result cells left over from a different harness version",
    )
    p_prune_stale.add_argument(
        "--results", default="results", help="Directory to read result JSON files from"
    )

    # list
    p_list = sub.add_parser("list", help="List models and their status")
    p_list.add_argument("--models", default="models.yaml")

    # snapshot
    p_snap = sub.add_parser(
        "snapshot",
        help="Materialize task fixtures from a pinned git commit (real repo state)",
    )
    p_snap.add_argument("--tasks", default="tasks", help="Path to tasks directory")
    p_snap.add_argument(
        "--repo",
        default=str(Path.home() / "repos" / "homelab"),
        help="Path to the source git repo",
    )
    p_snap.add_argument(
        "task", nargs="?", help="Only snapshot this task id (default: all)"
    )

    return parser


async def _run(args) -> None:
    """Run benchmark cells for every active (task, model) pair."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("Error: OPENROUTER_API_KEY environment variable is not set")
        return

    tasks = load_tasks(Path(args.tasks))
    reg = load_registry(Path(args.models))
    models = active_models(reg, include_experimental=args.include_experimental)

    # Optional filters for cheap, targeted calibration runs.
    if getattr(args, "task", None):
        wanted = {t.strip() for t in args.task.split(",") if t.strip()}
        tasks = [t for t in tasks if t.id in wanted]
    if getattr(args, "model_filter", None):
        models = [m for m in models if args.model_filter in m.id]
    if not tasks or not models:
        print("Nothing to run after filtering (check --task / --model).")
        return

    client = OpenRouterClient(api_key=api_key)
    await client.load_prices()

    sem = asyncio.Semaphore(args.concurrency)

    async def _deterministic_cell(task: TaskSpec, model, key: str) -> ResultCell | None:
        """Run a 2-shot deterministic cell via run_cell."""
        task_fixture_dir = Path(args.tasks) / task.id / "fixture"
        if task_fixture_dir.exists():
            fdir = task_fixture_dir
            cleanup = False
        else:
            fdir = Path(tempfile.mkdtemp())
            cleanup = True

        try:

            async def complete(**kw):
                return await client.complete(**kw)

            verify = get_verifier(task.verifier.kind)
            return await run_cell(
                task_id=task.id,
                task_version=task.version,
                model_id=model.id,
                content_hash=key,
                fixture_dir=fdir,
                target_files=task.target_files,
                prompt=task.prompt,
                complete=complete,
                verify=verify,
                verifier_args=task.verifier.args,
                cost_fn=lambda p, c: client.cost_usd(model.id, p, c),
                max_tokens=model.params.max_tokens,
            )
        finally:
            if cleanup:
                shutil.rmtree(fdir, ignore_errors=True)

    async def _agent_cell(task: TaskSpec, model, key: str) -> ResultCell | None:
        """Run an agentic (tool-calling) cell: the model edits the fixture, then grade.

        The fixture must be materialized (via `bench snapshot`) because the model
        explores and edits real repo state through file tools rather than emitting a
        whole file in one shot.
        """
        fixture_dir = Path(args.tasks) / task.id / "fixture"
        if not fixture_dir.exists():
            return ResultCell(
                task_id=task.id,
                task_version=task.version,
                model_id=model.id,
                content_hash=key,
                outcome="fail",
                attempts=[
                    Attempt(
                        passed=False,
                        feedback=(
                            f"[verifier setup] no fixture at {fixture_dir}; "
                            "run `python3 -m bench snapshot` first"
                        ),
                        latency_ms=0,
                        prompt_tokens=0,
                        completion_tokens=0,
                    )
                ],
                cost_usd=0.0,
                harness_version=HARNESS_VERSION,
                prompt_template_hash="agent",
                turns=0,  # mark as agentic so it stays out of the single-shot tables
                tool_use_ok=False,
            )

        async def chat(**kw):
            return await client.chat(**kw)

        verify = get_verifier(task.verifier.kind)
        return await run_agent_cell(
            task_id=task.id,
            task_version=task.version,
            model_id=model.id,
            content_hash=key,
            fixture_dir=fixture_dir,
            task_prompt=task.prompt,
            chat=chat,
            verify=verify,
            verifier_args=task.verifier.args,
            cost_fn=lambda p, c: client.cost_usd(model.id, p, c),
            max_turns=task.agent.max_turns,
            max_tokens=task.agent.max_tokens or model.params.max_tokens,
        )

    async def _judge_cell(task: TaskSpec, model, key: str) -> ResultCell | None:
        """Run a single-shot judge cell: candidate from the model, verdict from judge."""
        c1 = await client.complete(
            model=model.id,
            messages=[{"role": "user", "content": task.prompt}],
            temperature=0.0,
        )
        cfg = JudgeConfig(
            judge_model=task.verifier.args["judge_model"],
            criteria=task.verifier.args["criteria"],
        )

        # Synchronous caller for the judge model, required by judge_free_text's
        # Callable[[str], str] signature. Using httpx.Client (sync) here avoids
        # nesting asyncio.run inside a running loop.
        def sync_caller(prompt: str) -> str:
            import httpx as _httpx

            with _httpx.Client(timeout=120.0) as hc:
                resp = hc.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": cfg.judge_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.0,
                        "max_tokens": int(os.environ.get("JUDGE_MAX_TOKENS", "256")),
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]

        try:
            result = await asyncio.to_thread(
                judge_free_text,
                candidate=_strip_code_fence(c1.text),
                task_prompt=task.prompt,
                cfg=cfg,
                caller=sync_caller,
                candidate_model=model.id,
            )
        except ValueError as exc:
            print(f"[skip] {model.id} / {task.id}: {exc}")
            return None

        outcome: str = "pass@1" if result.passed else "fail"
        prompt_template_hash = hashlib.sha256(task.prompt.encode()).hexdigest()[:8]
        attempt = Attempt(
            passed=result.passed,
            feedback=str(result.votes),
            latency_ms=c1.latency_ms,
            prompt_tokens=c1.prompt_tokens,
            completion_tokens=c1.completion_tokens,
        )
        cost = client.cost_usd(model.id, c1.prompt_tokens, c1.completion_tokens)
        return ResultCell(
            task_id=task.id,
            task_version=task.version,
            model_id=model.id,
            content_hash=key,
            outcome=outcome,
            attempts=[attempt],
            cost_usd=cost,
            harness_version=HARNESS_VERSION,
            prompt_template_hash=prompt_template_hash,
        )

    async def _cell(task: TaskSpec, model) -> None:
        """Compute and store one (task, model) cell, skipping if cached."""
        task_fixture_dir = Path(args.tasks) / task.id / "fixture"
        fx = fixture_hash(task_fixture_dir) if task_fixture_dir.exists() else ""
        if task.mode == "agentic":
            # Fold mode + agent budget into the key so bumping turns/tokens re-runs.
            # Temperature is intentionally omitted: the agentic loop always runs at
            # temperature 0 (deterministic benchmarking), so model.params.temperature
            # is not an input and must not spuriously invalidate the cache.
            agent_max_tokens = task.agent.max_tokens or model.params.max_tokens
            params_repr = f"agentic:{agent_max_tokens}:turns={task.agent.max_turns}"
        else:
            params_repr = f"{model.params.temperature}:{model.params.max_tokens}"
        src_hash = (
            verifier_source_hash(task.verifier.kind)
            if task.verifier.kind != "judge"
            else "judge"
        )
        verifier_repr = json.dumps(
            {"kind": task.verifier.kind, "args": task.verifier.args, "src": src_hash},
            sort_keys=True,
        )
        key = cell_key(
            prompt=task.prompt,
            fixture_hash=fx,
            verifier_repr=verifier_repr,
            model_id=model.id,
            params_repr=params_repr,
        )
        path = cell_path(Path(args.results), model.id, task.id, key)

        if is_cached(path) and not args.force:
            print(f"[skip] {model.id} / {task.id} (cached)")
            return

        async with sem:
            try:
                if task.mode == "agentic":
                    cell = await _agent_cell(task, model, key)
                elif task.verifier.kind == "judge":
                    cell = await _judge_cell(task, model, key)
                else:
                    cell = await _deterministic_cell(task, model, key)

                if cell is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(cell.model_dump_json(indent=2))
                    print(f"[{cell.outcome}] {model.id} / {task.id}")
            except Exception as e:
                prompt_template_hash = hashlib.sha256(task.prompt.encode()).hexdigest()[
                    :8
                ]
                fail_cell = ResultCell(
                    task_id=task.id,
                    task_version=task.version,
                    model_id=model.id,
                    content_hash=key,
                    outcome="fail",
                    attempts=[
                        Attempt(
                            passed=False,
                            feedback=f"[harness error] {type(e).__name__}: {e}",
                            latency_ms=0,
                            prompt_tokens=0,
                            completion_tokens=0,
                        )
                    ],
                    cost_usd=0.0,
                    harness_version=HARNESS_VERSION,
                    prompt_template_hash=prompt_template_hash,
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(fail_cell.model_dump_json(indent=2))
                logger.warning("harness error for %s / %s: %s", model.id, task.id, e)

    coroutines = [_cell(task, model) for task in tasks for model in models]
    results = await asyncio.gather(*coroutines, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.warning("unhandled gather exception: %s", r)
    await client.aclose()


def _report(args) -> None:
    """Generate the leaderboard markdown from cached result JSON files."""
    tasks = load_tasks(Path(args.tasks))
    task_class_of = {t.id: t.task_class.value for t in tasks}

    reg = load_registry(Path(args.models))
    anchor_ids = {m.id for m in anchors(reg)}

    results_root = Path(args.results)
    cells: list[ResultCell] = []
    stale = 0
    if results_root.exists():
        for json_path in results_root.rglob("*.json"):
            try:
                cell = ResultCell.model_validate_json(json_path.read_text())
            except Exception as exc:
                logger.warning("skipping %s: %s", json_path, exc)
                continue
            # Only aggregate cells from the current harness version. Results from a
            # prior version (e.g. before a bug fix bumped HARNESS_VERSION, or under an
            # old model slug) linger on disk next to fresh cells; counting both would
            # double-count and mix corrupt data into the leaderboard.
            if cell.harness_version != HARNESS_VERSION:
                stale += 1
                continue
            cells.append(cell)

    if stale:
        logger.warning(
            "ignored %d result cell(s) from a different harness version (current %s); "
            "run `python -m bench prune-stale` to delete them",
            stale,
            HARNESS_VERSION,
        )

    # Single-shot and agentic are separate contracts: only single-shot cells feed the
    # per-class Budget/Anchors/Pareto tables. Agentic cells (turns is not None) have
    # their own headline table below, so counting them here would inflate a model's
    # single-shot code-fix score with a tool-calling pass.
    single_shot_cells = [c for c in cells if c.turns is None]
    agg = aggregate_by_class(single_shot_cells, task_class_of)

    anchor_agg = {mid: cls_map for mid, cls_map in agg.items() if mid in anchor_ids}
    cand_agg = {mid: cls_map for mid, cls_map in agg.items() if mid not in anchor_ids}

    # Deterministic single anchor: prefer id containing "sonnet", else smallest id.
    if anchor_ids:
        sonnet_anchors = sorted(a for a in anchor_ids if "sonnet" in a.lower())
        anchor_id = sonnet_anchors[0] if sonnet_anchors else sorted(anchor_ids)[0]
    else:
        anchor_id = None

    # Build per_class for candidates: model_id -> class -> {pass1, cost, tier, qualifies}
    per_class: dict[str, dict[str, dict]] = {}
    for model_id, cls_map in cand_agg.items():
        per_class[model_id] = {}
        for cls, score in cls_map.items():
            anchor_score = anchor_agg.get(anchor_id, {}).get(cls) if anchor_id else None
            per_class[model_id][cls] = {
                "pass1": score.pass1,
                "cost": score.cost,
                "tier": coarse_tier(score.pass1, score.pass2),
                "qualifies": qualifies(score, anchor_score)
                if anchor_score is not None
                else False,
            }

    # Build anchors_dict: model_id -> class -> {pass1, cost}
    anchors_dict: dict[str, dict[str, dict]] = {}
    for model_id, cls_map in anchor_agg.items():
        anchors_dict[model_id] = {}
        for cls, score in cls_map.items():
            anchors_dict[model_id][cls] = {"pass1": score.pass1, "cost": score.cost}

    # Pareto frontier per class across candidates only.
    classes: set[str] = set()
    for cls_map in cand_agg.values():
        classes.update(cls_map.keys())
    frontier: dict[str, list[str]] = {}
    for cls in classes:
        points = {
            mid: (cls_map[cls].pass1, cls_map[cls].cost)
            for mid, cls_map in cand_agg.items()
            if cls in cls_map
        }
        frontier[cls] = sorted(pareto_frontier(points))

    # Retired tombstones: one row per retired model with aggregated scores.
    retired_models = [m for m in reg if m.status == "retired"]
    retired: list[dict] = []
    for m in retired_models:
        all_scores = list(agg.get(m.id, {}).values())
        if all_scores:
            mean_pass1 = sum(s.pass1 for s in all_scores) / len(all_scores)
            mean_cost = sum(s.cost for s in all_scores) / len(all_scores)
        else:
            mean_pass1 = 0.0
            mean_cost = 0.0
        retired.append(
            {
                "id": m.id,
                "reason": m.retired_reason or "",
                "date": m.retired_date or "",
                "pass1": mean_pass1,
                "cost": mean_cost,
            }
        )

    # Agentic leaderboard: aggregate tool-calling cells (turns is not None) per model.
    from statistics import median

    agentic_groups: dict[str, list[ResultCell]] = {}
    for cell in cells:
        if cell.turns is not None:
            agentic_groups.setdefault(cell.model_id, []).append(cell)
    agentic: dict[str, dict] = {}
    for model_id, group in agentic_groups.items():
        n = len(group)
        agentic[model_id] = {
            "n": n,
            "pass_rate": sum(1 for c in group if c.first_attempt_passed) / n,
            "med_tokens": float(median([c.total_tokens for c in group])),
            "med_turns": float(median([c.turns or 0 for c in group])),
            "cost": sum(c.cost_usd for c in group) / n,
            "tool_ok_rate": sum(1 for c in group if c.tool_use_ok) / n,
        }

    md = render_leaderboard(
        per_class=per_class,
        anchors=anchors_dict,
        frontier=frontier,
        retired=retired,
        agentic=agentic,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    print(f"Report written to {out_path}")

    json_out = getattr(args, "json_out", None)
    if json_out:
        _write_leaderboard_json(
            Path(json_out),
            agentic=agentic,
            cells=cells,
            tasks=tasks,
            anchor_ids=anchor_ids,
            generated_at=getattr(args, "generated_at", None)
            or datetime.date.today().isoformat(),
        )
        print(f"Leaderboard JSON written to {json_out}")


def _write_leaderboard_json(
    path: Path,
    *,
    agentic: dict,
    cells: list,
    tasks: list,
    anchor_ids: set,
    generated_at: str,
) -> None:
    """Write the structured agentic leaderboard consumed by the public page.

    Self-contained snapshot: models ranked by pass-rate then cost, plus per-task
    pass counts and light provenance, so the SvelteKit page renders from a single
    committed file with no backend.
    """
    agentic_ids = {t.id for t in tasks if t.mode == "agentic"}
    task_meta = {t.id: t for t in tasks}

    # Per-task pass counts over agentic cells.
    per_task: dict[str, dict] = {}
    for cell in cells:
        if cell.turns is None or cell.task_id not in agentic_ids:
            continue
        d = per_task.setdefault(cell.task_id, {"passed": 0, "n": 0})
        d["n"] += 1
        d["passed"] += int(cell.first_attempt_passed)

    def _blurb(tid: str) -> str:
        if tid not in task_meta:
            return ""
        # First sentence of the prompt, trimmed, as a one-line task description.
        first = task_meta[tid].prompt.strip().split("\n")[0]
        sentence = first.split(". ")[0].strip()
        return (sentence[:157] + "...") if len(sentence) > 160 else sentence

    tasks_json = [
        {
            "id": tid,
            "class": task_meta[tid].task_class.value if tid in task_meta else "",
            "verifier": task_meta[tid].verifier.kind if tid in task_meta else "",
            # A "pytest" verifier grades with the repo's own gold test; "command" is a
            # synthetic behavioral check. Surfaced so the page can flag real-test tasks.
            "real_test": (
                tid in task_meta and task_meta[tid].verifier.kind == "pytest"
            ),
            "blurb": _blurb(tid),
            "source_commit": (
                task_meta[tid].source_commit if tid in task_meta else None
            ),
            "passed": v["passed"],
            "n": v["n"],
        }
        for tid, v in sorted(per_task.items())
    ]

    models_json = [
        {
            "id": mid,
            "role": "anchor" if mid in anchor_ids else "candidate",
            "n": s["n"],
            "pass_rate": round(s["pass_rate"], 4),
            "median_tokens": int(s["med_tokens"]),
            "median_turns": s["med_turns"],
            "cost_usd": round(s["cost"], 6),
            "tool_use_ok": round(s["tool_ok_rate"], 4),
        }
        for mid, s in agentic.items()
    ]
    # Best first: pass-rate desc, then cost asc, then tokens asc.
    models_json.sort(key=lambda r: (-r["pass_rate"], r["cost_usd"], r["median_tokens"]))

    payload = {
        "generated_at": generated_at,
        "harness_version": HARNESS_VERSION,
        "tasks": tasks_json,
        "models": models_json,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _drop(args) -> None:
    """Retire a model in the registry, recording today's date."""
    drop_model(
        Path(args.models),
        args.model,
        reason=args.reason,
        date=datetime.date.today().isoformat(),
    )
    print(f"Model {args.model!r} retired.")


def _prune(args) -> None:
    """Delete result directories for all retired models."""
    reg = load_registry(Path(args.models))
    pruned = prune_retired(Path(args.results), reg)
    print(pruned)


def _prune_stale(args) -> None:
    """Delete result cells whose harness_version differs from the current one."""
    results_root = Path(args.results)
    removed = 0
    if results_root.exists():
        for json_path in results_root.rglob("*.json"):
            try:
                cell = ResultCell.model_validate_json(json_path.read_text())
            except Exception as exc:
                logger.warning("skipping unreadable cell %s: %s", json_path, exc)
                continue
            if cell.harness_version != HARNESS_VERSION:
                json_path.unlink()
                removed += 1
    print(
        f"Removed {removed} stale result cell(s) (kept harness version {HARNESS_VERSION})."
    )


def _snapshot(args) -> None:
    """Materialize task fixtures from a pinned git commit.

    A task.yaml may carry a `snapshot: {commit, paths}` block. This extracts those
    paths from the source repo at that commit into the task's fixture/ directory, so
    fixtures are real repo state and can be regenerated or bumped to a newer commit
    later by editing the commit and re-running snapshot.
    """
    import subprocess

    repo = Path(args.repo)
    tasks_dir = Path(args.tasks)
    for subdir in sorted(tasks_dir.iterdir()):
        task_file = subdir / "task.yaml"
        if not task_file.exists():
            continue
        mapping = _load_yaml_mapping(task_file)
        if args.task and mapping.get("id") != args.task:
            continue
        snap = mapping.get("snapshot")
        if not isinstance(snap, dict):
            continue
        commit, paths = snap.get("commit"), snap.get("paths", [])
        if not commit or not paths:
            print(f"{mapping.get('id')}: snapshot needs commit + paths; skipping")
            continue
        fixture = subdir / "fixture"
        if fixture.exists():
            shutil.rmtree(fixture)
        fixture.mkdir(parents=True)
        # git archive <commit> -- <paths> | tar -x -C fixture
        archive = subprocess.run(
            ["git", "-C", str(repo), "archive", commit, "--", *paths],
            capture_output=True,
            check=True,
            timeout=120,
        )
        tar_cmd = ["tar", "-x", "-C", str(fixture)]
        strip = snap.get("strip_components")
        if strip:
            tar_cmd.append(f"--strip-components={strip}")
        subprocess.run(tar_cmd, input=archive.stdout, check=True, timeout=120)
        # Prune excluded globs (default: the parent's own *_test.py). The model never
        # needs them (the gold test is injected by the verifier), they bloat the
        # fixture, and committing real test files trips the repo's pre-commit semgrep
        # hook (generic-test-filename / hardcoded-timestamp rules).
        import fnmatch

        excludes = snap.get("exclude", ["*_test.py"])
        for path in sorted(fixture.rglob("*"), reverse=True):
            if path.is_file() and any(fnmatch.fnmatch(path.name, g) for g in excludes):
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        n = sum(1 for _ in fixture.rglob("*") if _.is_file())
        print(
            f"{mapping.get('id')}: snapshotted {n} file(s) from {commit[:12]} into {fixture}"
        )


def _list(args) -> None:
    """Print each model: id, status, role."""
    reg = load_registry(Path(args.models))
    for m in reg:
        print(f"{m.id}\t{m.status}\t{m.role}")


def main(argv=None) -> None:
    """Parse arguments and dispatch to the appropriate subcommand handler."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        asyncio.run(_run(args))
    elif args.command == "report":
        _report(args)
    elif args.command == "drop":
        _drop(args)
    elif args.command == "prune":
        _prune(args)
    elif args.command == "prune-stale":
        _prune_stale(args)
    elif args.command == "list":
        _list(args)
    elif args.command == "snapshot":
        _snapshot(args)
    else:
        parser.print_help()
