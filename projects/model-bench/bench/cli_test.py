import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest  # noqa: F401
import yaml

from bench.cache import HARNESS_VERSION
from bench.cli import (
    _aggregate_agentic_group,
    _parse_headers,
    _prune_stale,
    _report,
    _resolve_snapshot_preset,
    _snapshot,
    _write_leaderboard_json,
    build_parser,
    load_tasks,
)
from bench.schema import Attempt, ResultCell, TaskSpec, VerifierSpec
from bench.verifiers import get_verifier
from bench.verifiers.decision_conflict import CHART_PATH, PROTECTED_PATHS


def _real_decision_task():
    return (
        Path(__file__).parent.parent
        / "tasks/decision-conflict-chart-version-01/task.yaml"
    )


def test_cli_policy_opt_in_changes_only_its_cell_key(tmp_path, monkeypatch):
    import asyncio

    from bench import cli
    from bench.cache import cell_key
    from bench.schema import AgentConfig, ModelSpec

    model = ModelSpec(id="anchor", provider="claude-code")
    task = TaskSpec(
        id="t",
        version="1",
        task_class="code-fix",
        mode="agentic",
        prompt="p",
        verifier=VerifierSpec(kind="json-match"),
    )
    tasks = tmp_path / "tasks"
    (tasks / "t" / "fixture").mkdir(parents=True)
    monkeypatch.setattr(cli, "load_tasks", lambda root: [task])
    monkeypatch.setattr(cli, "load_registry", lambda root: [model])
    parameters = []
    keys = []

    def key(**kwargs):
        parameters.append(kwargs["params_repr"])
        result = cell_key(**kwargs)
        keys.append(result)
        return result

    monkeypatch.setattr(cli, "cell_key", key)

    def anchor(**kwargs):
        assert (
            kwargs["repository_policy_precedence"]
            == task.agent.repository_policy_precedence
        )
        return ResultCell(
            task_id="t",
            task_version="1",
            model_id="anchor",
            content_hash=kwargs["content_hash"],
            outcome="pass@1",
            attempts=[],
            cost_usd=0,
            harness_version=HARNESS_VERSION,
            prompt_template_hash="agent",
        )

    monkeypatch.setattr(cli.claude_code, "run_anchor_agent_cell", anchor)
    for policy in (False, True, False):
        task.agent = AgentConfig(repository_policy_precedence=policy)
        args = build_parser().parse_args(
            [
                "run",
                "--tasks",
                str(tasks),
                "--results",
                str(tmp_path / "results"),
                "--force",
            ]
        )
        asyncio.run(cli._run(args))
    legacy = "agentic:8192:turns=20:exec=False:provider=claude-code:api_model=anchor:extra={}"
    assert parameters == [
        legacy,
        legacy.replace(":provider", ":repository_policy_precedence=True:provider"),
        legacy,
    ]
    assert keys[0] == keys[2] and keys[0] != keys[1]


def test_real_decision_task_loads_through_taskspec(tmp_path):
    task_file = _real_decision_task()
    task_dir = tmp_path / "decision-conflict-chart-version-01"
    task_dir.mkdir()
    task_dir.joinpath("task.yaml").write_bytes(task_file.read_bytes())
    (task,) = load_tasks(tmp_path)
    assert task.id == "decision-conflict-chart-version-01"
    assert task.mode == "agentic"
    assert task.agent.repository_policy_precedence is True
    assert callable(get_verifier(task.verifier.kind))
    mapping = yaml.safe_load(task_file.read_text())
    assert isinstance(mapping, dict)
    assert mapping["snapshot"]["commit"] == "7caf01130404326f26782b3bdb59873c8ab1390e"
    assert mapping["snapshot"]["strip_components"] == 0
    assert set(mapping["snapshot"]["paths"]) == set(PROTECTED_PATHS)
    assert set(task.verifier.args["file_sha256"]) == set(PROTECTED_PATHS)
    assert "precedence" not in task.prompt and "ARCHITECTURE.md" not in task.prompt


def test_snapshot_materializes_small_fixture_from_temporary_git(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    files = {
        CHART_PATH: "apiVersion: v2\nname: test\nversion: 3.2.0\n",
        "AGENTS.md": "Follow architecture decisions.\n",
        "projects/platform/ARCHITECTURE.md": "CI writes chart versions after merge.\n",
    }
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    vendored = repo / "projects/mcp/context-forge-gateway/chart/charts/unwanted.tgz"
    vendored.parent.mkdir()
    vendored.write_bytes(b"not selected")
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init")
    git("add", ".")
    git("commit", "-m", "test: fixture")
    commit = git("rev-parse", "HEAD")
    mapping = yaml.safe_load(_real_decision_task().read_text())
    assert isinstance(mapping, dict)
    mapping["snapshot"]["commit"] = commit
    mapping["verifier"]["args"]["file_sha256"] = {
        rel: hashlib.sha256(text.encode()).hexdigest() for rel, text in files.items()
    }
    tasks = tmp_path / "tasks"
    pack = tasks / mapping["id"]
    pack.mkdir(parents=True)
    pack.joinpath("task.yaml").write_text(yaml.safe_dump(mapping))
    _snapshot(argparse.Namespace(repo=str(repo), tasks=str(tasks), task=mapping["id"]))
    fixture = pack / "fixture"
    assert {
        p.relative_to(fixture).as_posix() for p in fixture.rglob("*") if p.is_file()
    } == set(files)
    (task,) = load_tasks(tasks)
    result = get_verifier(task.verifier.kind)(
        fixture,
        task.verifier.args,
        final_response=(
            "I refuse to bump the chart version. projects/platform/ARCHITECTURE.md "
            "says chart-version-bot writes versions after merge."
        ),
    )
    assert result.passed, result.feedback


def test_resolve_snapshot_preset_expands_and_lets_task_override():
    # A bare preset expands to the canonical full-backend paths/exclude.
    resolved = _resolve_snapshot_preset({"preset": "monolith-backend", "commit": "abc"})
    assert resolved["paths"] == ["projects/monolith"]
    assert resolved["commit"] == "abc"
    assert "frontend/" in resolved["exclude"]
    assert "*_test.py" in resolved["exclude"]
    assert "preset" not in resolved
    # A task key wins over the preset (here: a custom strip_components).
    over = _resolve_snapshot_preset(
        {"preset": "monolith-backend", "commit": "x", "strip_components": 9}
    )
    assert over["strip_components"] == 9
    # No preset: returned unchanged.
    plain = {"commit": "z", "paths": ["a"]}
    assert _resolve_snapshot_preset(plain) == plain


def test_resolve_snapshot_preset_unknown_raises():
    with pytest.raises(ValueError, match="unknown snapshot preset"):
        _resolve_snapshot_preset({"preset": "nope", "commit": "x"})


def test_parser_has_subcommands():
    p = build_parser()
    choices = p._subparsers._group_actions[0].choices
    for sub in ("run", "report", "drop", "prune", "prune-stale", "list", "snapshot"):
        assert sub in choices


def test_run_parser_accepts_base_url_and_timeout():
    p = build_parser()
    args = p.parse_args(
        [
            "run",
            "--base-url",
            "http://127.0.0.1:18080/v1",
            "--timeout",
            "900",
            "--model",
            "qwen3.8-27b",
        ]
    )
    assert args.base_url == "http://127.0.0.1:18080/v1"
    assert args.timeout == 900.0
    assert args.model_filter == "qwen3.8-27b"


def test_run_parser_collects_repeated_headers():
    """Two --header flags are needed together: Cloudflare Access checks both."""
    p = build_parser()
    args = p.parse_args(
        [
            "run",
            "--base-url",
            "https://private.jomcgi.dev/llm/v1",
            "--header",
            "CF-Access-Client-Id: abc.access",
            "--header",
            "CF-Access-Client-Secret: s3cret",
        ]
    )
    assert _parse_headers(args.header) == {
        "CF-Access-Client-Id": "abc.access",
        "CF-Access-Client-Secret": "s3cret",
    }


def test_parse_headers_splits_on_first_colon_only():
    """A value may contain colons; only the first separates name from value."""
    assert _parse_headers(["X-Origin: https://example.com:8443/x"]) == {
        "X-Origin": "https://example.com:8443/x"
    }


def test_parse_headers_rejects_malformed_entry():
    """Raise rather than skip: a dropped secret reads as a 401, not as a typo."""
    with pytest.raises(ValueError):
        _parse_headers(["CF-Access-Client-Id"])


def test_prune_stale_removes_only_other_versions(tmp_path, capsys):
    root = tmp_path / "results" / "m" / "t1"
    root.mkdir(parents=True)
    cur = (
        '{"task_id":"t1","task_version":"v1","model_id":"m","content_hash":"aaa",'
        '"outcome":"pass@1","attempts":[],"cost_usd":0.0,'
        f'"harness_version":"{HARNESS_VERSION}","prompt_template_hash":"x"}}'
    )
    old = cur.replace(
        f'"harness_version":"{HARNESS_VERSION}"', '"harness_version":"0.0.0"'
    )
    (root / "cur.json").write_text(cur)
    (root / "old.json").write_text(old)
    _prune_stale(argparse.Namespace(results=str(tmp_path / "results")))
    assert (root / "cur.json").exists()
    assert not (root / "old.json").exists()


def test_load_tasks_reads_pack(tmp_path):
    d = tmp_path / "tasks" / "t1"
    d.mkdir(parents=True)
    (d / "task.yaml").write_text(
        "id: t1\nversion: v1\nclass: config-plumbing\nprompt: p\n"
        'target_files: [values.yaml]\nverifier: {kind: command, args: {cmd: ["true"]}}\n'
    )
    tasks = load_tasks(tmp_path / "tasks")
    assert tasks[0].id == "t1" and tasks[0].task_class == "config-plumbing"


def _agentic_cell(
    task_id,
    model_id,
    passed,
    turns,
    tokens,
    tool_ok,
    *,
    feedback="",
    cost=0.01,
    latency_ms=1,
):
    return ResultCell(
        task_id=task_id,
        task_version="v1",
        model_id=model_id,
        content_hash="h",
        outcome="pass@1" if passed else "fail",
        attempts=[
            Attempt(
                passed=passed,
                feedback=feedback,
                latency_ms=latency_ms,
                prompt_tokens=tokens,
                completion_tokens=0,
            )
        ],
        cost_usd=cost,
        harness_version=HARNESS_VERSION,
        prompt_template_hash="agent",
        turns=turns,
        tool_use_ok=tool_ok,
    )


def test_write_leaderboard_json_shape_and_ranking(tmp_path):
    task = TaskSpec(
        id="worldcup-fixtures-guard-01",
        version="v1",
        task_class="code-fix",
        mode="agentic",
        prompt="Fix parse_fixtures so unresolved rows are dropped. Second sentence.",
        verifier=VerifierSpec(kind="pytest"),
        source_commit="abc123",
    )
    cells = [
        _agentic_cell("worldcup-fixtures-guard-01", "cheap/win", True, 4, 1000, True),
        _agentic_cell("worldcup-fixtures-guard-01", "anchor/x", True, 3, 2000, True),
    ]
    gate = {
        "floor_n": 1,
        "floor_pass": 1,
        "floor_failed": [],
        "qualified": True,
        "hard_n": 0,
        "hard_pass": 0,
        "errored": 0,
        "errored_tasks": [],
    }
    agentic = {
        "cheap/win": {
            "n": 1,
            "pass_rate": 1.0,
            "mean_tokens": 1000.0,
            "mean_turns": 4.0,
            "mean_latency_ms": 8000.0,
            "cost": 0.001,
            "cost_per_solve": 0.001,
            "tool_ok_rate": 1.0,
            **gate,
        },
        "anchor/x": {
            "n": 1,
            "pass_rate": 1.0,
            "mean_tokens": 2000.0,
            "mean_turns": 3.0,
            "mean_latency_ms": 20000.0,
            "cost": 0.5,
            "cost_per_solve": 0.5,
            "tool_ok_rate": 1.0,
            **gate,
        },
    }
    out = tmp_path / "leaderboard.json"
    _write_leaderboard_json(
        out,
        agentic=agentic,
        cells=cells,
        tasks=[task],
        anchor_ids={"anchor/x"},
        generated_at="2026-07-01",
    )
    data = json.loads(out.read_text())
    assert data["generated_at"] == "2026-07-01"
    # Cheapest of two equal-pass models ranks first; anchor role is tagged.
    assert data["models"][0]["id"] == "cheap/win"
    assert data["models"][1]["role"] == "anchor"
    # Display name falls back to the id minus the provider prefix when unset.
    assert data["models"][0]["name"] == "win"
    # Value fields surfaced: wall-time and cost-per-solve.
    assert data["models"][0]["mean_latency_ms"] == 8000
    assert data["models"][0]["cost_per_solve_usd"] == 0.001
    assert data["models"][0]["errored"] == 0
    assert data["models"][0]["errored_tasks"] == []
    # Per-task breakdown is embedded for the deep-dive: one entry per graded task,
    # carrying pass/fail plus the per-task tokens and turns.
    (mt,) = data["models"][0]["tasks"]
    assert mt["id"] == "worldcup-fixtures-guard-01"
    assert mt["passed"] is True and mt["tokens"] == 1000 and mt["turns"] == 4
    # Per-task cost is carried too, for the scatter's per-task Cloud view.
    assert mt["cost_usd"] == 0.01
    # The one agentic task appears with its real-test flag, blurb, and pass count.
    (t,) = data["tasks"]
    assert t["id"] == "worldcup-fixtures-guard-01"
    assert t["real_test"] is True and t["passed"] == 2 and t["n"] == 2
    assert t["blurb"] and "Second sentence." not in t["blurb"]


def test_aggregate_agentic_group_excludes_harness_errors_from_all_metrics():
    cells = [
        _agentic_cell("floor-pass", "m", True, 2, 100, True, cost=0.01),
        _agentic_cell("hard-fail", "m", False, 4, 300, False, cost=0.03),
        _agentic_cell(
            "floor-error",
            "m",
            False,
            99,
            999,
            False,
            feedback="prefix [harness error] HTTPStatusError",
            cost=9.0,
            latency_ms=999,
        ),
    ]

    stats = _aggregate_agentic_group(
        cells,
        {"floor-pass": "easy", "floor-error": "standard", "hard-fail": "hard"},
    )

    assert stats["n"] == 2
    assert stats["errored"] == 1
    assert stats["errored_tasks"] == ["floor-error"]
    assert stats["pass_rate"] == 0.5
    assert stats["floor_pass"] == 1 and stats["floor_n"] == 1
    assert stats["floor_failed"] == [] and stats["qualified"] is True
    assert stats["hard_pass"] == 0 and stats["hard_n"] == 1
    assert stats["mean_tokens"] == 200.0
    assert stats["mean_turns"] == 3.0
    assert stats["mean_latency_ms"] == 1.0
    assert stats["cost"] == pytest.approx(0.02)
    assert stats["cost_per_solve"] == pytest.approx(0.04)
    assert stats["tool_ok_rate"] == 0.5


def test_aggregate_agentic_group_all_errored_is_zeroed_and_disqualified():
    cell = _agentic_cell(
        "floor-error",
        "m",
        False,
        5,
        500,
        False,
        feedback="[harness error] context overflow",
        cost=2.0,
    )

    stats = _aggregate_agentic_group([cell], {"floor-error": "easy"})

    assert stats == {
        "n": 0,
        "pass_rate": 0.0,
        "floor_n": 0,
        "floor_pass": 0,
        "floor_failed": [],
        "qualified": False,
        "hard_n": 0,
        "hard_pass": 0,
        "mean_tokens": 0.0,
        "mean_turns": 0.0,
        "mean_latency_ms": 0.0,
        "cost": 0.0,
        "cost_per_solve": None,
        "tool_ok_rate": 0.0,
        "errored": 1,
        "errored_tasks": ["floor-error"],
    }


def test_report_reprices_cells_for_models_with_a_fixed_rate(tmp_path):
    """A self-hosted cell recorded at $0 reports at the registry's rate."""
    (tmp_path / "models.yaml").write_text(
        "models:\n"
        "  - id: local/m\n"
        "    status: experimental\n"
        "    self_hosted: true\n"
        "    price: {input_per_million: 0.10, output_per_million: 0.20}\n"
    )
    task_dir = tmp_path / "tasks" / "t1"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        "id: t1\nversion: v1\nclass: code-fix\nmode: agentic\ntier: hard\n"
        'prompt: p\nverifier: {kind: command, args: {cmd: ["true"]}}\n'
    )
    cell = ResultCell(
        task_id="t1",
        task_version="v1",
        model_id="local/m",
        content_hash="h",
        outcome="pass@1",
        attempts=[
            Attempt(
                passed=True,
                feedback="",
                latency_ms=1,
                prompt_tokens=1_000_000,
                completion_tokens=500_000,
            )
        ],
        cost_usd=0.0,
        harness_version=HARNESS_VERSION,
        prompt_template_hash="agent",
        turns=3,
        tool_use_ok=True,
    )
    cell_dir = tmp_path / "results" / "local__m" / "t1"
    cell_dir.mkdir(parents=True)
    (cell_dir / "h.json").write_text(cell.model_dump_json())
    out = tmp_path / "lb.json"
    _report(
        argparse.Namespace(
            results=str(tmp_path / "results"),
            models=str(tmp_path / "models.yaml"),
            tasks=str(tmp_path / "tasks"),
            out=str(tmp_path / "lb.md"),
            json_out=str(out),
            generated_at="2026-10-02",
        )
    )
    row = json.loads(out.read_text())["models"][0]
    # 1M input at $0.10 plus 0.5M output at $0.20.
    assert row["cost_usd"] == pytest.approx(0.2)
    assert row["tasks"][0]["cost_usd"] == pytest.approx(0.2)
    assert row["self_hosted"] is True
