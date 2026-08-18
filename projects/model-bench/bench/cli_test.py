import argparse
import json

import pytest  # noqa: F401

from bench.cache import HARNESS_VERSION
from bench.cli import (
    _parse_headers,
    _prune_stale,
    _resolve_snapshot_preset,
    _write_leaderboard_json,
    build_parser,
    load_tasks,
)
from bench.schema import Attempt, ResultCell, TaskSpec, VerifierSpec


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


def _agentic_cell(task_id, model_id, passed, turns, tokens, tool_ok):
    return ResultCell(
        task_id=task_id,
        task_version="v1",
        model_id=model_id,
        content_hash="h",
        outcome="pass@1" if passed else "fail",
        attempts=[
            Attempt(
                passed=passed,
                feedback="",
                latency_ms=1,
                prompt_tokens=tokens,
                completion_tokens=0,
            )
        ],
        cost_usd=0.01,
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
