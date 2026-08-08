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

import yaml

from bench import claude_code
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

logger = logging.getLogger(__name__)


# Raw result cells (one JSON per (task, model) run) are the expensive, billed output
# of a calibration run. They MUST NOT live inside the git worktree: they are gitignored,
# so a `git worktree remove` deletes them and forces a full paid re-run. Default them to
# a stable per-user cache dir that survives worktree churn; override with
# MODEL_BENCH_RESULTS or --results. This machine is the only place the bench runs.
_DEFAULT_RESULTS = os.environ.get("MODEL_BENCH_RESULTS") or str(
    Path.home() / ".cache" / "model-bench" / "results"
)


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
        "--results",
        default=_DEFAULT_RESULTS,
        help="Directory to store result JSON files",
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
    p_report.add_argument("--results", default=_DEFAULT_RESULTS)
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
    p_drop.add_argument("--models", default="models.yaml", help="Path to models.yaml")
    p_drop.add_argument("--reason", required=True, help="Short retirement reason")

    # prune
    p_prune = sub.add_parser("prune", help="Delete result files for retired models")
    p_prune.add_argument("--models", default="models.yaml", help="Path to models.yaml")
    p_prune.add_argument(
        "--results",
        default=_DEFAULT_RESULTS,
        help="Directory to read result JSON files from",
    )

    # prune-stale
    p_prune_stale = sub.add_parser(
        "prune-stale",
        help="Delete result cells left over from a different harness version",
    )
    p_prune_stale.add_argument(
        "--results",
        default=_DEFAULT_RESULTS,
        help="Directory to read result JSON files from",
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

    # OpenRouter is only needed for openrouter-backed candidates. An anchors-only run
    # (provider=claude-code) uses the local `claude` CLI and needs no key, so we only
    # demand the key and open a client when a candidate actually requires it.
    needs_openrouter = any(m.provider == "openrouter" for m in models)
    if needs_openrouter and not api_key:
        print("Error: OPENROUTER_API_KEY environment variable is not set")
        return

    client = None
    if needs_openrouter:
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
            if model.provider == "claude-code":
                complete = claude_code.complete

                def cost_fn(p, c):
                    return 0.0  # free under Max

            else:

                async def complete(**kw):
                    return await client.complete(**kw)

                def cost_fn(p, c):
                    return client.cost_usd(model.id, p, c)

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
                cost_fn=cost_fn,
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

        verify = get_verifier(task.verifier.kind)

        # Anchors run agentically inside Claude Code's own harness (its native tools),
        # not the bench's OpenRouter tool loop, and are graded by the same verifier. It
        # blocks on a subprocess, so run it off the event loop.
        if model.provider == "claude-code":
            return await asyncio.to_thread(
                claude_code.run_anchor_agent_cell,
                task_id=task.id,
                task_version=task.version,
                model_id=model.id,
                content_hash=key,
                fixture_dir=fixture_dir,
                task_prompt=task.prompt,
                verify=verify,
                verifier_args=task.verifier.args,
            )

        async def chat(**kw):
            return await client.chat(**kw)

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
            allow_exec=task.agent.exec,
        )

    async def _judge_cell(task: TaskSpec, model, key: str) -> ResultCell | None:
        """Run a single-shot judge cell: candidate from the model, verdict from judge.

        The candidate uses its own provider (an openrouter model still bills for its one
        completion); the judge always runs via the local `claude` CLI, so judging is free
        and no longer an OpenRouter cost.
        """
        if model.provider == "claude-code":
            c1 = await claude_code.complete(
                model=model.id,
                messages=[{"role": "user", "content": task.prompt}],
                temperature=0.0,
            )
            candidate_cost = 0.0
        else:
            c1 = await client.complete(
                model=model.id,
                messages=[{"role": "user", "content": task.prompt}],
                temperature=0.0,
            )
            candidate_cost = client.cost_usd(
                model.id, c1.prompt_tokens, c1.completion_tokens
            )
        cfg = JudgeConfig(
            judge_model=task.verifier.args["judge_model"],
            criteria=task.verifier.args["criteria"],
        )

        try:
            result = await asyncio.to_thread(
                judge_free_text,
                candidate=_strip_code_fence(c1.text),
                task_prompt=task.prompt,
                cfg=cfg,
                caller=claude_code.judge_caller,
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
        cost = candidate_cost
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
            params_repr = (
                f"agentic:{agent_max_tokens}:turns={task.agent.max_turns}"
                f":exec={task.agent.exec}"
            )
        else:
            params_repr = f"{model.params.temperature}:{model.params.max_tokens}"
        # Fold the provider into the key: a model switched from openrouter to claude-code
        # (an anchor moving to the free Claude Code harness) produces a materially
        # different cell (different harness, cost=0), so its stale openrouter result must
        # not be reused.
        params_repr = f"{params_repr}:provider={model.provider}"
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
    if client is not None:
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
    superseded = 0
    # Keep only the NEWEST cell per (model, task). A cell's cache key includes the fixture
    # hash, so changing a task's fixture (e.g. migrating it to a bigger snapshot) writes a
    # NEW cell file next to the old one under the same harness version. Both would be
    # aggregated and the model's task would be double/triple-counted with stale data. The
    # freshest file on disk is the current fixture's result, so newest-wins dedup keeps the
    # board and the public page showing only current results without deleting anything.
    newest: dict[tuple[str, str], tuple[float, ResultCell]] = {}
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
            mtime = json_path.stat().st_mtime
            key = (cell.model_id, cell.task_id)
            prev = newest.get(key)
            if prev is None or mtime > prev[0]:
                if prev is not None:
                    superseded += 1
                newest[key] = (mtime, cell)
            else:
                superseded += 1
    cells = [c for _, c in newest.values()]

    if stale:
        logger.warning(
            "ignored %d result cell(s) from a different harness version (current %s); "
            "run `python -m bench prune-stale` to delete them",
            stale,
            HARNESS_VERSION,
        )
    if superseded:
        logger.warning(
            "ignored %d superseded cell(s) (older fixture for a (model, task) that was "
            "re-run); newest-on-disk wins",
            superseded,
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

    # Agentic leaderboard: aggregate tool-calling cells (turns is not None) per model,
    # under the gate model. easy + standard tasks form the qualification FLOOR: pointed,
    # basic-viability tasks that ~90%+ of candidates clear. A model qualifies if it misses
    # at most FLOOR_MISS_TOLERANCE of them, so one flaky floor miss does not exclude an
    # otherwise-strong model while a model that fails several basics is still gated out.
    # hard tasks (real-tree navigation / net-new building) differentiate the qualified;
    # perf/efficiency is the value axis among them.
    from statistics import mean

    FLOOR_MISS_TOLERANCE = 1

    # Only count cells for tasks that are still in the current task set. Cells for a
    # task that was later removed or renamed linger in the durable results cache; the
    # harness-version filter above does not catch them (same version, dropped task),
    # so without this scope a stale task would silently inflate every model's n and
    # skew the medians away from the published task set. This mirrors the per-task and
    # per-model breakdowns below, which are already scoped to current tasks.
    current_agentic_ids = {t.id for t in tasks if t.mode == "agentic"}
    tier_of = {t.id: t.tier for t in tasks}
    FLOOR_TIERS = {"easy", "standard"}
    # Retired models are excluded from the leaderboard even if their cells linger in the
    # durable cache, so `bench drop` alone removes a model without needing a cell prune.
    retired_ids = {m.id for m in reg if m.status == "retired"}
    agentic_groups: dict[str, list[ResultCell]] = {}
    for cell in cells:
        if (
            cell.turns is not None
            and cell.task_id in current_agentic_ids
            and cell.model_id not in retired_ids
        ):
            agentic_groups.setdefault(cell.model_id, []).append(cell)
    agentic: dict[str, dict] = {}
    for model_id, group in agentic_groups.items():
        n = len(group)
        pass_rate = sum(1 for c in group if c.first_attempt_passed) / n
        cost = sum(c.cost_usd for c in group) / n
        floor = [c for c in group if tier_of.get(c.task_id) in FLOOR_TIERS]
        hard = [c for c in group if tier_of.get(c.task_id) == "hard"]
        floor_failed = sorted(c.task_id for c in floor if not c.first_attempt_passed)
        agentic[model_id] = {
            "n": n,
            "pass_rate": pass_rate,
            # Gate metrics.
            "floor_n": len(floor),
            "floor_pass": sum(1 for c in floor if c.first_attempt_passed),
            "floor_failed": floor_failed,
            # Qualified iff it ran floor tasks and missed at most the tolerance.
            "qualified": bool(floor) and len(floor_failed) <= FLOOR_MISS_TOLERANCE,
            "hard_n": len(hard),
            "hard_pass": sum(1 for c in hard if c.first_attempt_passed),
            # Mean (not median) per task: the tasks vary ~5x in size, and a model can
            # blow up on one hard task (e.g. a greenfield build) while looking tidy on
            # the median. The mean keeps that tail visible, and it matches how `cost`
            # below is already aggregated, so all the efficiency columns tell one story.
            "mean_tokens": float(mean([c.total_tokens for c in group])),
            "mean_turns": float(mean([c.turns or 0 for c in group])),
            # Mean end-to-end wall-time per task (ms). This is the CLOUD lens: what a
            # request to this model actually costs in time via OpenRouter. It does NOT
            # transfer to self-hosted 4090 throughput (different HW/quant/batching), but
            # with cost it is the real value signal for offloading work off a paid tier.
            "mean_latency_ms": float(mean([c.total_latency_ms for c in group])),
            "cost": cost,
            # Cost per SOLVED task, so a cheap-but-flaky model does not look like a
            # bargain. Infinite when nothing passes (rendered as a sentinel).
            "cost_per_solve": (cost / pass_rate) if pass_rate > 0 else None,
            "tool_ok_rate": sum(1 for c in group if c.tool_use_ok) / n,
        }

    md = render_leaderboard(
        per_class=per_class,
        anchors=anchors_dict,
        frontier=frontier,
        retired=retired,
        agentic=agentic,
        # Anchors live in the same `agentic` dict (so the page JSON still lists them),
        # but the markdown splits them into a ceiling section instead of the cost-ranked
        # candidate tables, where their free (cost=0) rows would otherwise dominate.
        agentic_anchor_ids=anchor_ids,
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
            display_names={m.id: m.display_name for m in reg if m.display_name},
            generated_at=getattr(args, "generated_at", None)
            or datetime.date.today().isoformat(),
        )
        print(f"Leaderboard JSON written to {json_out}")


def _short_name(model_id: str) -> str:
    """Display name minus the provider prefix (e.g. qwen/foo -> foo)."""
    return model_id.split("/", 1)[1] if "/" in model_id else model_id


def _write_leaderboard_json(
    path: Path,
    *,
    agentic: dict,
    cells: list,
    tasks: list,
    anchor_ids: set,
    generated_at: str,
    display_names: dict | None = None,
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
    # Per-(model, task) breakdown so the page can render a deep-dive on each model
    # row: which of the tasks it passed, and the tokens/turns/wall-time it spent on
    # each. This is the same agentic cells, just kept disaggregated instead of only
    # rolled up into the model's medians.
    per_model_tasks: dict[str, dict[str, dict]] = {}
    for cell in cells:
        if cell.turns is None or cell.task_id not in agentic_ids:
            continue
        d = per_task.setdefault(cell.task_id, {"passed": 0, "n": 0})
        d["n"] += 1
        d["passed"] += int(cell.first_attempt_passed)
        # Last cell wins if a (model, task) somehow has duplicates on disk; within one
        # harness version there is exactly one cached cell per pair.
        per_model_tasks.setdefault(cell.model_id, {})[cell.task_id] = {
            "id": cell.task_id,
            "passed": cell.first_attempt_passed,
            "tokens": cell.total_tokens,
            "turns": cell.turns,
            "latency_ms": cell.total_latency_ms,
            # Per-task cost so the scatter's per-task Cloud view has a real $ per point,
            # not just the model-level mean.
            "cost_usd": round(cell.cost_usd, 6),
        }

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
            # A task with a source_commit snapshots the parent of a real fix commit and
            # grades with that commit's own gold test (SWE-bench style). Tasks without one
            # (slo, flights) are synthetic / hand-authored. Surfaced so the page can flag
            # which rows are graded by the repo's real tests.
            "real_test": (
                tid in task_meta and task_meta[tid].source_commit is not None
            ),
            "tier": task_meta[tid].tier if tid in task_meta else "standard",
            "blurb": _blurb(tid),
            "source_commit": (
                task_meta[tid].source_commit if tid in task_meta else None
            ),
            "passed": v["passed"],
            "n": v["n"],
        }
        for tid, v in sorted(per_task.items())
    ]

    names = display_names or {}
    models_json = [
        {
            "id": mid,
            "name": names.get(mid) or _short_name(mid),
            "role": "anchor" if mid in anchor_ids else "candidate",
            "n": s["n"],
            "pass_rate": round(s["pass_rate"], 4),
            # Gate model: qualified iff every floor (easy/standard) task passed.
            "qualified": s["qualified"],
            "floor_pass": s["floor_pass"],
            "floor_n": s["floor_n"],
            "floor_failed": s["floor_failed"],
            "hard_pass": s["hard_pass"],
            "hard_n": s["hard_n"],
            "mean_tokens": int(s["mean_tokens"]),
            "mean_turns": round(s["mean_turns"], 2),
            "mean_latency_ms": int(s["mean_latency_ms"]),
            "cost_usd": round(s["cost"], 6),
            "cost_per_solve_usd": (
                round(s["cost_per_solve"], 6)
                if s.get("cost_per_solve") is not None
                else None
            ),
            "tool_use_ok": round(s["tool_ok_rate"], 4),
            # Per-task breakdown, ordered by task id so it lines up with tasks_json.
            "tasks": [
                per_model_tasks[mid][tid]
                for tid in sorted(per_model_tasks.get(mid, {}))
            ],
        }
        for mid, s in agentic.items()
    ]
    # Best first: qualified above disqualified, then most hard tasks solved, then
    # cheapest, then fastest. The page reads `qualified` to split the two sections.
    models_json.sort(
        key=lambda r: (
            not r["qualified"],
            -r["hard_pass"],
            r["cost_usd"],
            r["mean_latency_ms"],
        )
    )

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


# Named fixture presets: the single source of truth for a "real repo subtree" fixture,
# so tasks that share a fixture shape don't each repeat a long paths+exclude block (the
# duplication this whole migration removes). A task references one with
# `snapshot: {preset: monolith-backend, commit: <sha>}` and may still override any field.
# Each preset snapshots a whole real project dir and prunes it to the language of interest,
# so it works at ANY commit (no per-commit package list): `git archive` grabs whatever
# existed then, and the excludes drop the rest.
_SNAPSHOT_PRESETS: dict[str, dict] = {
    # The full monolith Python backend: every feature domain + shared infra, minus the
    # frontend, Helm chart, deploy manifests, tests (the gold test is injected), and all
    # non-.py data. The model must navigate the real tree to find the domain it edits.
    "monolith-backend": {
        "paths": ["projects/monolith"],
        "strip_components": 2,
        "exclude": [
            "frontend/",
            "chart/",
            "deploy/",
            "node_modules/",
            "e2e/",
            "*_test.py",
            "*.md",
            "*.json",
            "*.ndjson",
            "*.sql",
            "*.sh",
            "*.jpg",
            "*.jpeg",
            "*.png",
            "*.yaml",
            "*.yml",
            "*.txt",
            "*.lock",
            "*.bzl",
            "BUILD",
        ],
    },
}


def _resolve_snapshot_preset(snap: dict) -> dict:
    """Expand a `preset` into concrete paths/strip_components/exclude.

    A task's own keys win over the preset (so a task can override the commit, add an
    extra exclude, etc.). Unknown preset names raise so a typo fails loudly rather than
    silently snapshotting nothing.
    """
    preset_name = snap.get("preset")
    if not preset_name:
        return snap
    if preset_name not in _SNAPSHOT_PRESETS:
        raise ValueError(
            f"unknown snapshot preset {preset_name!r}; "
            f"known: {sorted(_SNAPSHOT_PRESETS)}"
        )
    return {
        **_SNAPSHOT_PRESETS[preset_name],
        **{k: v for k, v in snap.items() if k != "preset"},
    }


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
        snap = _resolve_snapshot_preset(snap)
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
        # Prune excludes (default: the parent's own *_test.py). The model never needs
        # them (the gold test is injected by the verifier), they bloat the fixture, and
        # committing real test files trips the repo's pre-commit semgrep hook.
        #
        # Two exclude forms: a plain glob matches a file BASENAME (`*_test.py`, `*.json`);
        # an entry ending in `/` is a DIRECTORY exclude that drops everything under any
        # directory of that name (`frontend/`, `chart/`). The directory form is what makes
        # `paths: [projects/monolith]` viable as one commit-agnostic "full backend" spec:
        # snapshot the whole tree, then drop the non-backend subtrees.
        import fnmatch

        excludes = snap.get("exclude", ["*_test.py"])
        dir_excludes = {e.rstrip("/") for e in excludes if e.endswith("/")}
        name_globs = [e for e in excludes if not e.endswith("/")]
        for path in sorted(fixture.rglob("*"), reverse=True):
            if path.is_file():
                parents = set(path.relative_to(fixture).parts[:-1])
                if (dir_excludes & parents) or any(
                    fnmatch.fnmatch(path.name, g) for g in name_globs
                ):
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
