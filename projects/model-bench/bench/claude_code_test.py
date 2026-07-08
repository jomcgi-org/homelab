"""Tests for the Claude Code subprocess backend.

The real `claude` CLI is never spawned: `_invoke` (agentic runs) and `subprocess.run`
(the cmd-construction test) are monkeypatched so the tests are hermetic.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from bench import claude_code


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
        verify=lambda workdir, args: _VerifyResult(False, "route missing"),
        verifier_args={},
    )
    assert cell.outcome == "fail"
    assert "route missing" in cell.attempts[0].feedback


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
