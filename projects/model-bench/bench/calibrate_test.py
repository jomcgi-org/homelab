import datetime
import json

import pytest
import yaml

from bench import calibrate, claude_code
from bench.cli import main
from bench.schema import ModelSpec, TaskSpec, VerifierSpec

REG = [
    ModelSpec(
        id="anthropic/claude-haiku-4.5",
        role="anchor",
        provider="claude-code",
        api_model="claude-haiku-4-5-20251001",
    ),
    ModelSpec(
        id="anthropic/claude-sonnet-5.5-cc", role="anchor", provider="claude-code"
    ),
    ModelSpec(id="anthropic/claude-opus-5.5", role="anchor", provider="claude-code"),
    ModelSpec(id="qwen/x"),
]

TASK_YAML = """\
# a comment that must survive --write
id: t
version: v1
class: code-fix
tier: hard
mode: agentic
prompt: write answer.json
verifier:
  kind: json-match
  args:
    file: answer.json
    expect: {ok: true}
"""


def _fake_invoke(results):
    """A fake `claude -p`: writes answer.json in cwd per the model's scripted result."""

    def fake(prompt, *, model=None, cwd=None, **kw):
        outcome = results[model]
        if outcome == "error":
            raise RuntimeError("cli crashed")
        (cwd / "answer.json").write_text(json.dumps({"ok": outcome}))
        return claude_code.ClaudeResult(text="", num_turns=1, is_error=False, wall_ms=1)

    return fake


def test_resolve_anchors_maps_aliases_and_rejects_candidates():
    got = calibrate.resolve_anchors(REG, ["haiku", "opus"])
    assert got["haiku"].id == "anthropic/claude-haiku-4.5"
    with pytest.raises(ValueError):
        calibrate.resolve_anchors(REG, ["qwen/x"])


def test_run_ladder_scores_each_run_and_pins_the_model(monkeypatch, tmp_path):
    (tmp_path / "seed.txt").write_text("x")
    monkeypatch.setattr(
        claude_code,
        "_invoke",
        _fake_invoke(
            {
                "claude-haiku-4-5-20251001": "error",
                "claude-sonnet-5-5-cc": False,
                "claude-opus-5-5": True,
            }
        ),
    )
    task = TaskSpec(
        id="t",
        version="v1",
        **{"class": "code-fix"},
        prompt="p",
        verifier=VerifierSpec(
            kind="json-match", args={"file": "answer.json", "expect": {"ok": True}}
        ),
    )
    anchors = calibrate.resolve_anchors(REG, ["haiku", "sonnet", "opus"])
    scores = calibrate.run_ladder(task, tmp_path, anchors, reps=2, jobs=3)
    # A CLI crash is ungraded (None), not a zero; binary verifiers score 1/0.
    assert scores == {"haiku": [None, None], "sonnet": [0.0, 0.0], "opus": [1.0, 1.0]}


def test_check_ladder_admits_a_monotone_ladder():
    summary = calibrate.summarize(
        {"haiku": [0.3, 0.5], "sonnet": [0.7, 0.9], "opus": [1.0, 0.95]}
    )
    assert summary["haiku"]["mean"] == 0.4
    admitted, reasons = calibrate.check_ladder(summary, None)
    # Sonnet's 0.8 mean sits on the top of its band.
    assert admitted, reasons


def test_check_ladder_rejects_a_saturated_task_and_honours_overrides():
    summary = calibrate.summarize({"haiku": [1.0], "sonnet": [1.0], "opus": [1.0]})
    admitted, reasons = calibrate.check_ladder(summary, None)
    assert not admitted
    assert any("haiku" in r for r in reasons)
    assert any("increasing" in r for r in reasons)
    loose = {"haiku_max": 1.0, "sonnet_max": 1.0}
    summary = calibrate.summarize({"haiku": [0.5], "sonnet": [0.9], "opus": [1.0]})
    assert calibrate.check_ladder(summary, loose)[0]


def test_check_ladder_needs_graded_runs_for_every_anchor():
    summary = calibrate.summarize({"haiku": [None], "sonnet": [0.5], "opus": [1.0]})
    admitted, reasons = calibrate.check_ladder(summary, None)
    assert not admitted and reasons == ["haiku: no graded runs"]


def test_write_calibration_replaces_the_block_and_keeps_comments(tmp_path):
    task_file = tmp_path / "task.yaml"
    task_file.write_text(TASK_YAML + "calibration:\n  date: old\n  verdict: reject\n")
    summary = calibrate.summarize({"haiku": [0.2], "sonnet": [0.6], "opus": [1.0]})
    record = calibrate.calibration_record(
        summary,
        True,
        [],
        source="test",
        ladder=None,
        today=datetime.date(2026, 10, 2),
    )
    calibrate.write_calibration(task_file, record)
    text = task_file.read_text()
    assert text.startswith("# a comment that must survive --write\n")
    assert "date: old" not in text and text.count("calibration:") == 1
    loaded = TaskSpec.model_validate(yaml.safe_load(text))
    assert loaded.calibration["verdict"] == "admit"
    assert loaded.calibration["scores"]["opus"]["mean"] == 1.0


def test_calibrate_cli_records_external_scores(tmp_path, capsys):
    task_dir = tmp_path / "tasks" / "t"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(TASK_YAML)
    scores = tmp_path / "scores.json"
    scores.write_text(
        json.dumps({"haiku": [0.2, 0.3], "sonnet": [0.6, 0.7], "opus": [1, 0.95]})
    )
    main(
        [
            "calibrate",
            "--task",
            "t",
            "--tasks",
            str(tmp_path / "tasks"),
            "--from-json",
            str(scores),
            "--write",
        ]
    )
    out = capsys.readouterr().out
    assert "verdict: ADMIT" in out
    cal = yaml.safe_load((task_dir / "task.yaml").read_text())["calibration"]
    assert cal["verdict"] == "admit" and cal["source"] == "external (scores.json)"
