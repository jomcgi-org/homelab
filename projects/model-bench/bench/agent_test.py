import asyncio

import pytest  # noqa: F401

from bench.agent import _execute_tool, run_agent_cell
from bench.openrouter import ChatResult
from bench.verifiers import VerifyResult


def test_execute_tool_read_write_list(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    assert "a.py" in _execute_tool("list_dir", {"path": "."}, tmp_path)
    assert _execute_tool("read_file", {"path": "a.py"}, tmp_path) == "x = 1\n"
    assert "wrote" in _execute_tool(
        "write_file", {"path": "a.py", "content": "x = 2\n"}, tmp_path
    )
    assert (tmp_path / "a.py").read_text() == "x = 2\n"


def test_execute_tool_rejects_path_escape(tmp_path):
    out = _execute_tool("read_file", {"path": "../../etc/passwd"}, tmp_path)
    assert "outside the repo" in out


def test_run_agent_cell_edits_then_grades(tmp_path):
    (tmp_path / "f.py").write_text("BAD\n")

    # Scripted agent: turn 1 writes the fix, turn 2 calls done.
    script = [
        {
            "tool_calls": [
                {
                    "id": "1",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path": "f.py", "content": "GOOD"}',
                    },
                }
            ]
        },
        {"tool_calls": [{"id": "2", "function": {"name": "done", "arguments": "{}"}}]},
    ]
    calls = {"i": 0}

    async def fake_chat(**kwargs):
        msg = script[calls["i"]]
        calls["i"] += 1
        return ChatResult(
            message=msg, prompt_tokens=5, completion_tokens=3, latency_ms=2
        )

    def verify(workdir, args):
        return VerifyResult((workdir / "f.py").read_text() == "GOOD", "not fixed")

    cell = asyncio.run(
        run_agent_cell(
            task_id="t",
            task_version="v1",
            model_id="m",
            content_hash="h",
            fixture_dir=tmp_path,
            task_prompt="fix f.py",
            chat=fake_chat,
            verify=verify,
            verifier_args={},
            cost_fn=lambda p, c: 0.0,
        )
    )
    assert cell.outcome == "pass@1"
    assert cell.total_tokens == 16  # (5+3) across two turns
    assert cell.turns == 2  # wrote on turn 1, done on turn 2
    assert cell.tool_use_ok is True


def test_run_agent_cell_flags_model_that_never_calls_a_tool(tmp_path):
    (tmp_path / "f.py").write_text("BAD\n")

    async def fake_chat(**kwargs):
        # The model just talks; it never emits a tool call, so it cannot drive the loop.
        return ChatResult(
            message={"content": "I think the fix is easy."},
            prompt_tokens=4,
            completion_tokens=2,
            latency_ms=1,
        )

    def verify(workdir, args):
        return VerifyResult((workdir / "f.py").read_text() == "GOOD", "not fixed")

    cell = asyncio.run(
        run_agent_cell(
            task_id="t",
            task_version="v1",
            model_id="m",
            content_hash="h",
            fixture_dir=tmp_path,
            task_prompt="fix f.py",
            chat=fake_chat,
            verify=verify,
            verifier_args={},
            cost_fn=lambda p, c: 0.0,
        )
    )
    assert cell.outcome == "fail"
    assert cell.tool_use_ok is False
    assert "no tool calls emitted" in cell.attempts[0].feedback


def test_run_agent_cell_flags_malformed_tool_arguments(tmp_path):
    (tmp_path / "f.py").write_text("BAD\n")

    script = [
        # Turn 1: a write_file call whose arguments are not valid JSON.
        {
            "tool_calls": [
                {
                    "id": "1",
                    "function": {"name": "write_file", "arguments": "{not json"},
                }
            ]
        },
        {"tool_calls": [{"id": "2", "function": {"name": "done", "arguments": "{}"}}]},
    ]
    calls = {"i": 0}

    async def fake_chat(**kwargs):
        msg = script[calls["i"]]
        calls["i"] += 1
        return ChatResult(
            message=msg, prompt_tokens=1, completion_tokens=1, latency_ms=1
        )

    def verify(workdir, args):
        return VerifyResult(False, "not fixed")

    cell = asyncio.run(
        run_agent_cell(
            task_id="t",
            task_version="v1",
            model_id="m",
            content_hash="h",
            fixture_dir=tmp_path,
            task_prompt="fix f.py",
            chat=fake_chat,
            verify=verify,
            verifier_args={},
            cost_fn=lambda p, c: 0.0,
        )
    )
    # It drove the loop but a call was malformed -> reliability miss recorded.
    assert cell.tool_use_ok is False
    assert "malformed tool-call arguments" in cell.attempts[0].feedback
