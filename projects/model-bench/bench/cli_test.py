import argparse
import json

import pytest  # noqa: F401

from bench.cache import HARNESS_VERSION
from bench.cli import _prune_stale, _write_leaderboard_json, build_parser, load_tasks
from bench.schema import Attempt, ResultCell, TaskSpec, VerifierSpec


def test_parser_has_subcommands():
    p = build_parser()
    choices = p._subparsers._group_actions[0].choices
    for sub in ("run", "report", "drop", "prune", "prune-stale", "list", "snapshot"):
        assert sub in choices


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
    agentic = {
        "cheap/win": {
            "n": 1,
            "pass_rate": 1.0,
            "med_tokens": 1000.0,
            "med_turns": 4.0,
            "cost": 0.001,
            "tool_ok_rate": 1.0,
        },
        "anchor/x": {
            "n": 1,
            "pass_rate": 1.0,
            "med_tokens": 2000.0,
            "med_turns": 3.0,
            "cost": 0.5,
            "tool_ok_rate": 1.0,
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
    # The one agentic task appears with its real-test flag, blurb, and pass count.
    (t,) = data["tasks"]
    assert t["id"] == "worldcup-fixtures-guard-01"
    assert t["real_test"] is True and t["passed"] == 2 and t["n"] == 2
    assert t["blurb"] and "Second sentence." not in t["blurb"]
