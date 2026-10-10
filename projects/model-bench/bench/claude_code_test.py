"""Tests for the Claude Code subprocess backend.

The real `claude` CLI is never spawned: `_invoke` (agentic runs) and `subprocess.run`
(the cmd-construction test) are monkeypatched so the tests are hermetic.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from bench import claude_code


@pytest.mark.parametrize("passed", [True, False])
def test_anchor_norms_capture_precedes_hidden_verifier_writes(
    tmp_path, monkeypatch, passed
):
    from bench.verifiers import VerifyResult

    (tmp_path / "m.go").write_text("package m\nvar X = 1\n")
    dirs = []

    def invoke(*args, **kwargs):
        (kwargs["cwd"] / "m.go").write_text("package m\nvar X = 2\n")
        return claude_code.ClaudeResult("done", 1, False, 1)

    def verify(workdir, args):
        dirs.append(workdir)
        (workdir / "hidden_test.go").write_text("package m\n// SECRET GRADER\n")
        (workdir / "m.go").write_text("// overwritten by verifier\n")
        return VerifyResult(passed, "")

    monkeypatch.setattr(claude_code, "_invoke", invoke)
    cell = claude_code.run_anchor_agent_cell(
        task_id="t",
        task_version="v1",
        model_id="m",
        content_hash="h",
        fixture_dir=tmp_path,
        task_prompt="fix",
        verify=verify,
        verifier_args={},
        norms_opts={"lint": False, "target_files": ["m.go"]},
    )
    assert cell.first_attempt_passed is passed
    assert not dirs[0].exists()
    if passed:
        assert cell.norms["test_added"] is False
        assert cell.norms["files_changed"] == 1
        assert cell.norms["files_outside_targets"] == 0
        assert cell.norms["lines_added"] == cell.norms["lines_removed"] == 1
        assert "+var X = 2" in cell.diff
        assert "hidden" not in cell.diff and "GRADER" not in cell.diff
        assert "verifier" not in cell.diff
    else:
        assert cell.norms is None and cell.diff is None


@dataclass
class _FakeProc:
    returncode: int
    stdout: str
    stderr: str = ""


def test_invoke_parses_json_and_builds_edit_cmd(monkeypatch, tmp_path):
    """cwd set -> acceptEdits + allowedTools; result/num_turns parsed."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc(
            returncode=0,
            stdout=json.dumps({"result": "done", "num_turns": 4, "is_error": False}),
        )

    monkeypatch.setattr(claude_code.subprocess, "run", fake_run)
    res = claude_code._invoke(
        "do it", cwd=tmp_path, allowed_tools=["Edit", "Write"], timeout_s=5
    )
    assert res.text == "done"
    assert res.num_turns == 4
    assert res.is_error is False
    assert res.wall_ms >= 0
    assert "--permission-mode" in captured["cmd"]  # editing enabled
    assert "acceptEdits" in captured["cmd"]
    assert "Edit,Write" in captured["cmd"]
    assert captured["cwd"] == str(tmp_path)
    assert "--model" not in captured["cmd"]  # no model -> the CLI default


def test_invoke_pins_the_model(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc(returncode=0, stdout=json.dumps({"result": "ok"}))

    monkeypatch.setattr(claude_code.subprocess, "run", fake_run)
    claude_code._invoke("x", model="claude-opus-5-5")
    i = captured["cmd"].index("--model")
    assert captured["cmd"][i + 1] == "claude-opus-5-5"


def test_cli_model_maps_registry_ids():
    assert claude_code.cli_model("anthropic/claude-opus-4.8") == "claude-opus-4-8"
    assert (
        claude_code.cli_model("anthropic/claude-haiku-4.5", "claude-haiku-4-5-20251001")
        == "claude-haiku-4-5-20251001"
    )


def test_invoke_no_cwd_is_readonly(monkeypatch):
    """No cwd (judge / single-shot) -> no acceptEdits, no cwd."""

    def fake_run(cmd, **kwargs):
        assert kwargs.get("cwd") is None
        assert "--permission-mode" not in cmd
        return _FakeProc(returncode=0, stdout=json.dumps({"result": "verdict"}))

    monkeypatch.setattr(claude_code.subprocess, "run", fake_run)
    assert claude_code._invoke("judge this").text == "verdict"


def test_invoke_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        claude_code.subprocess,
        "run",
        lambda cmd, **kw: _FakeProc(returncode=1, stdout="", stderr="boom"),
    )
    try:
        claude_code._invoke("x")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "boom" in str(exc)


def test_flatten_single_message_passthrough():
    assert (
        claude_code._flatten_messages([{"role": "user", "content": "task"}]) == "task"
    )
    assert claude_code._flatten_messages([]) == ""


def test_flatten_shot2_keeps_task_and_prior_attempt():
    """A stateless retry must still carry the original task + prior attempt."""
    flat = claude_code._flatten_messages(
        [
            {"role": "user", "content": "ORIGINAL TASK"},
            {"role": "assistant", "content": "BAD ATTEMPT"},
            {"role": "user", "content": "failed validation, fix it"},
        ]
    )
    assert "ORIGINAL TASK" in flat
    assert "BAD ATTEMPT" in flat
    assert "previous attempt" in flat
    assert "fix it" in flat


def test_complete_flattens_full_conversation(monkeypatch):
    seen = {}

    def fake_invoke(prompt, **kw):
        seen["prompt"] = prompt
        return claude_code.ClaudeResult(
            text="fixed", num_turns=1, is_error=False, wall_ms=3
        )

    monkeypatch.setattr(claude_code, "_invoke", fake_invoke)
    res = asyncio.run(
        claude_code.complete(
            model="anthropic/claude-opus-4.8",
            messages=[
                {"role": "user", "content": "ORIGINAL TASK"},
                {"role": "assistant", "content": "BAD"},
                {"role": "user", "content": "fix it"},
            ],
        )
    )
    assert res.text == "fixed"
    # The subprocess saw the whole conversation, not just the last "fix it" note.
    assert "ORIGINAL TASK" in seen["prompt"]
    assert "BAD" in seen["prompt"]


def test_judge_caller_returns_text(monkeypatch):
    monkeypatch.setattr(
        claude_code,
        "_invoke",
        lambda prompt, **kw: claude_code.ClaudeResult(
            text="PASS", num_turns=1, is_error=False, wall_ms=10
        ),
    )
    assert claude_code.judge_caller("criteria...") == "PASS"


@dataclass
class _VerifyResult:
    passed: bool
    feedback: str
    score: float | None = None


def _make_fixture(tmp_path):
    fx = tmp_path / "fixture"
    fx.mkdir()
    (fx / "seed.py").write_text("x = 1\n")
    return fx


def test_anchor_cell_passes_and_is_free(monkeypatch, tmp_path):
    fx = _make_fixture(tmp_path)
    monkeypatch.setattr(
        claude_code,
        "_invoke",
        lambda prompt, **kw: claude_code.ClaudeResult(
            text="", num_turns=6, is_error=False, wall_ms=1234
        ),
    )
    cell = claude_code.run_anchor_agent_cell(
        task_id="t",
        task_version="v1",
        model_id="anthropic/claude-opus-4.8",
        content_hash="h",
        fixture_dir=fx,
        task_prompt="add a route",
        verify=lambda workdir, args: _VerifyResult(True, ""),
        verifier_args={},
    )
    assert cell.outcome == "pass@1"
    assert cell.cost_usd == 0.0  # free under Max
    assert cell.turns == 6
    assert cell.tool_use_ok is True
    assert cell.attempts[0].latency_ms == 1234
    assert cell.attempts[0].prompt_tokens == 0
    # A passing anchor cell carries norms (no edits here, so a clean 1.0).
    assert cell.norms is not None and cell.norms_score == 1.0


def test_anchor_cell_fails_when_verifier_fails(monkeypatch, tmp_path):
    fx = _make_fixture(tmp_path)
    monkeypatch.setattr(
        claude_code,
        "_invoke",
        lambda prompt, **kw: claude_code.ClaudeResult(
            text="", num_turns=2, is_error=False, wall_ms=5
        ),
    )
    cell = claude_code.run_anchor_agent_cell(
        task_id="t",
        task_version="v1",
        model_id="anthropic/claude-opus-4.8",
        content_hash="h",
        fixture_dir=fx,
        task_prompt="add a route",
        verify=lambda workdir, args: _VerifyResult(False, "route missing", 0.4),
        verifier_args={},
    )
    assert cell.outcome == "fail"
    assert "route missing" in cell.attempts[0].feedback
    assert cell.attempts[0].score == 0.4
    assert cell.norms is None  # norms are only scored above the pass floor


def test_anchor_cell_fails_on_cli_error(monkeypatch, tmp_path):
    fx = _make_fixture(tmp_path)
    monkeypatch.setattr(
        claude_code,
        "_invoke",
        lambda prompt, **kw: claude_code.ClaudeResult(
            text="context limit", num_turns=1, is_error=True, wall_ms=5
        ),
    )
    called = {"verify": False}

    def _verify(workdir, args):
        called["verify"] = True
        return _VerifyResult(True, "")

    cell = claude_code.run_anchor_agent_cell(
        task_id="t",
        task_version="v1",
        model_id="anthropic/claude-opus-4.8",
        content_hash="h",
        fixture_dir=fx,
        task_prompt="p",
        verify=_verify,
        verifier_args={},
    )
    assert cell.outcome == "fail"
    assert called["verify"] is False  # a CLI error short-circuits grading
    assert "is_error" in cell.attempts[0].feedback


def test_complete_uses_api_model_over_registry_id(monkeypatch):
    seen = {}

    def fake_invoke(prompt, **kw):
        seen["model"] = kw.get("model")
        return claude_code.ClaudeResult(
            text="ok", num_turns=1, is_error=False, wall_ms=1
        )

    monkeypatch.setattr(claude_code, "_invoke", fake_invoke)
    asyncio.run(
        claude_code.complete(
            model="anthropic/claude-sonnet-5.5-cc",
            api_model="claude-sonnet-5-5",
            messages=[{"role": "user", "content": "hi"}],
        )
    )
    assert seen["model"] == "claude-sonnet-5-5"


def test_anchor_cell_cleans_workdir_when_fixture_copy_fails(monkeypatch, tmp_path):
    import shutil
    import tempfile
    from pathlib import Path

    fx = _make_fixture(tmp_path)
    created = []
    real_mkdtemp = tempfile.mkdtemp

    def _mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    def _copytree(*args, **kwargs):
        raise OSError("fixture unreadable")

    monkeypatch.setattr(tempfile, "mkdtemp", _mkdtemp)
    monkeypatch.setattr(shutil, "copytree", _copytree)

    cell = claude_code.run_anchor_agent_cell(
        task_id="t",
        task_version="v1",
        model_id="anthropic/claude-opus-4.8",
        content_hash="h",
        fixture_dir=fx,
        task_prompt="p",
        verify=lambda workdir, args: _VerifyResult(True, ""),
        verifier_args={},
    )
    assert cell.outcome == "fail"
    assert "harness error" in cell.attempts[0].feedback
    assert created and all(not p.exists() for p in created)


def test_anchor_cell_grades_the_cli_final_message(monkeypatch, tmp_path):
    fx = _make_fixture(tmp_path)

    def invoke(prompt, **kw):
        # The anchor writes a PR.md with the right words; what it SAID is the response.
        (kw["cwd"] / "PR.md").write_text("chart-version-bot writes it back")
        return claude_code.ClaudeResult(
            text="Left the version alone.", num_turns=3, is_error=False, wall_ms=7
        )

    monkeypatch.setattr(claude_code, "_invoke", invoke)
    seen = {}
    verifier_args = {"weights": {"x": 1}}

    def verify(workdir, args):
        seen["args"] = args
        assert (workdir / "PR.md").exists()
        return _VerifyResult(True, "")

    cell = claude_code.run_anchor_agent_cell(
        task_id="t",
        task_version="v1",
        model_id="anthropic/claude-opus-4.8",
        content_hash="h",
        fixture_dir=fx,
        task_prompt="p",
        verify=verify,
        verifier_args=verifier_args,
    )
    assert cell.outcome == "pass@1"
    assert seen["args"]["response"] == "Left the version alone."
    assert seen["args"]["weights"] == {"x": 1}
    # The task's own args are passed through, not mutated in place.
    assert verifier_args == {"weights": {"x": 1}}
