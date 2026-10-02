import asyncio
import copy
import json

import pytest  # noqa: F401

from bench.agent import (
    AGENT_SYSTEM,
    REPOSITORY_POLICY_INSTRUCTION,
    TOOLS,
    _execute_tool,
    run_agent_cell,
)
from bench.openrouter import ChatResult
from bench.verifiers import VerifyResult


@pytest.mark.parametrize(
    "message,expected",
    [
        ({"content": "terminal explanation"}, "terminal explanation"),
        ({"content": None}, ""),
        ({"content": " \n "}, ""),
        ({}, ""),
        (
            {
                "content": "fallback",
                "tool_calls": [
                    {
                        "function": {
                            "name": "done",
                            "arguments": '{"summary":"completion summary"}',
                        }
                    }
                ],
            },
            "completion summary",
        ),
        (
            {
                "content": "fallback",
                "tool_calls": [
                    {"function": {"name": "done", "arguments": '{"summary":"  "}'}}
                ],
            },
            "fallback",
        ),
        (
            {
                "content": "fallback",
                "tool_calls": [
                    {"function": {"name": "done", "arguments": '{"summary":null}'}}
                ],
            },
            "fallback",
        ),
        ({"tool_calls": [{"function": {"name": "done", "arguments": "{}"}}]}, ""),
    ],
)
def test_agent_harness_captures_final_response(tmp_path, message, expected):
    seen = {}

    async def chat(**kwargs):
        seen["tools"] = copy.deepcopy(kwargs["tools"])
        seen["system"] = kwargs["messages"][0]["content"]
        return ChatResult(
            message=message, prompt_tokens=0, completion_tokens=0, latency_ms=0
        )

    def verify(workdir, args, *, final_response):
        seen["response"] = final_response
        return VerifyResult(bool(final_response), "empty completion")

    cell = asyncio.run(
        run_agent_cell(
            task_id="t",
            task_version="1",
            model_id="m",
            content_hash="h",
            fixture_dir=tmp_path,
            task_prompt="user prompt is not a final answer",
            chat=chat,
            verify=verify,
            verifier_args={},
            cost_fn=lambda p, c: 0,
            repository_policy_precedence=True,
        )
    )
    assert seen["response"] == expected
    assert cell.outcome == ("pass@1" if expected else "fail")
    assert seen["system"] == AGENT_SYSTEM + " " + REPOSITORY_POLICY_INSTRUCTION
    assert (
        seen["tools"][3]["function"]["parameters"]["properties"]["summary"]["type"]
        == "string"
    )
    assert TOOLS[3]["function"]["parameters"]["properties"] == {}


def test_agent_other_tasks_keep_system_schema_and_verifier_call(tmp_path):
    baseline = copy.deepcopy(TOOLS)

    async def chat(**kwargs):
        assert kwargs["tools"] == baseline
        assert kwargs["messages"][0] == {"role": "system", "content": AGENT_SYSTEM}
        return ChatResult(
            message={"content": "finished"},
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0,
        )

    def verify(*args, **kwargs):
        assert len(args) == 2 and kwargs == {}
        assert args[1] == {"sentinel": True}
        return VerifyResult(True, "")

    asyncio.run(
        run_agent_cell(
            task_id="old",
            task_version="1",
            model_id="m",
            content_hash="h",
            fixture_dir=tmp_path,
            task_prompt="old prompt",
            chat=chat,
            verify=verify,
            verifier_args={"sentinel": True},
            cost_fn=lambda p, c: 0,
        )
    )
    assert TOOLS == baseline


@pytest.mark.parametrize("arguments", ["null", "[]", '"finished"', "1"])
def test_run_agent_cell_done_with_non_object_arguments(tmp_path, arguments):
    # Valid JSON that is not an object must not crash the done handler: the
    # summary is empty and the legacy two-argument verifier still runs.
    seen = {}

    async def fake_chat(**kwargs):
        return ChatResult(
            message={
                "tool_calls": [
                    {"id": "1", "function": {"name": "done", "arguments": arguments}}
                ]
            },
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=1,
        )

    def verify(workdir, args):
        seen["called"] = True
        return VerifyResult(True, "")

    cell = asyncio.run(
        run_agent_cell(
            task_id="t",
            task_version="v1",
            model_id="m",
            content_hash="h",
            fixture_dir=tmp_path,
            task_prompt="p",
            chat=fake_chat,
            verify=verify,
            verifier_args={},
            cost_fn=lambda p, c: 0.0,
        )
    )
    assert seen.get("called") is True
    assert cell.outcome == "pass@1"
    assert "[harness error]" not in cell.attempts[0].feedback


def test_agent_tool_turn_text_is_not_a_completion(tmp_path):
    async def chat(**kwargs):
        return ChatResult(
            message={
                "content": "not terminal",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "answer.txt"}),
                        }
                    }
                ],
            },
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0,
        )

    (tmp_path / "answer.txt").write_text("model-written answer is not evidence")

    def verify(w, a, *, final_response):
        assert final_response == ""
        return VerifyResult(False, "empty completion")

    cell = asyncio.run(
        run_agent_cell(
            task_id="t",
            task_version="1",
            model_id="m",
            content_hash="h",
            fixture_dir=tmp_path,
            task_prompt="p",
            chat=chat,
            verify=verify,
            verifier_args={},
            cost_fn=lambda p, c: 0,
            max_turns=1,
        )
    )
    assert cell.outcome == "fail"


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


def test_execute_tool_run_executes_shell(tmp_path):
    out = _execute_tool("run", {"command": "echo hi && pwd"}, tmp_path)
    assert out.startswith("exit 0")
    assert "hi" in out


def test_execute_tool_run_rejects_empty_command(tmp_path):
    assert "error" in _execute_tool("run", {"command": "  "}, tmp_path)


def test_run_tool_gated_by_allow_exec(tmp_path):
    # The `run` tool is only offered when a task opts into exec; file-only tasks
    # keep their calibrated tool set.
    seen = {}

    async def fake_chat(**kwargs):
        seen["tool_names"] = [t["function"]["name"] for t in kwargs["tools"]]
        return ChatResult(
            message={
                "tool_calls": [
                    {"id": "1", "function": {"name": "done", "arguments": "{}"}}
                ]
            },
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=1,
        )

    for allow, want in ((False, False), (True, True)):
        asyncio.run(
            run_agent_cell(
                task_id="t",
                task_version="v1",
                model_id="m",
                content_hash="h",
                fixture_dir=tmp_path,
                task_prompt="p",
                chat=fake_chat,
                verify=lambda w, a: VerifyResult(True, ""),
                verifier_args={},
                cost_fn=lambda p, c: 0.0,
                allow_exec=allow,
            )
        )
        assert ("run" in seen["tool_names"]) is want


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
