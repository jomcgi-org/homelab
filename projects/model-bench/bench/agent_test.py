import asyncio

import pytest

from bench.agent import _execute_tool, run_agent_cell
from bench.openrouter import ChatResult
from bench.verifiers import VerifyResult


@pytest.mark.parametrize("passed", [True, False])
def test_norms_capture_precedes_hidden_verifier_writes(tmp_path, passed):
    (tmp_path / "m.py").write_text("x = 1\n")
    dirs = []

    async def chat(**kwargs):
        return ChatResult(
            message={
                "tool_calls": [
                    {
                        "id": "1",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"m.py","content":"x = 2\\n"}',
                        },
                    },
                    {"id": "2", "function": {"name": "done", "arguments": "{}"}},
                ]
            },
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=1,
        )

    def verify(workdir, args):
        dirs.append(workdir)
        (workdir / "test_hidden.py").write_text("# SECRET GRADER\nassert True\n")
        (workdir / "m.py").write_text("# verifier overwrote the model's edit\n")
        return VerifyResult(passed, "")

    cell = asyncio.run(
        run_agent_cell(
            task_id="t",
            task_version="v1",
            model_id="m",
            content_hash="h",
            fixture_dir=tmp_path,
            task_prompt="fix",
            chat=chat,
            verify=verify,
            verifier_args={},
            cost_fn=lambda p, c: 0,
            norms_opts={"lint": False, "target_files": ["m.py"]},
        )
    )
    assert cell.first_attempt_passed is passed
    assert not dirs[0].exists()
    if passed:
        assert cell.norms["test_added"] is False
        assert cell.norms["files_changed"] == 1
        assert cell.norms["files_outside_targets"] == 0
        assert cell.norms["lines_added"] == cell.norms["lines_removed"] == 1
        assert "+x = 2" in cell.diff
        assert "hidden" not in cell.diff and "GRADER" not in cell.diff
        assert "verifier" not in cell.diff
    else:
        assert cell.norms is None and cell.diff is None


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


def test_run_agent_cell_records_graded_score(tmp_path):
    async def fake_chat(**kwargs):
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

    cell = asyncio.run(
        run_agent_cell(
            task_id="t",
            task_version="v1",
            model_id="m",
            content_hash="h",
            fixture_dir=tmp_path,
            task_prompt="p",
            chat=fake_chat,
            verify=lambda w, a: VerifyResult(False, "killed 1/4 mutants", 0.25),
            verifier_args={},
            cost_fn=lambda p, c: 0.0,
        )
    )
    assert cell.outcome == "fail"
    assert cell.attempts[0].score == 0.25


def test_run_agent_cell_scores_norms_only_on_a_pass(tmp_path):
    (tmp_path / "f.py").write_text("x = 1\n")

    async def fake_chat(**kwargs):
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

    def run(passed):
        return asyncio.run(
            run_agent_cell(
                task_id="t",
                task_version="v1",
                model_id="m",
                content_hash="h",
                fixture_dir=tmp_path,
                task_prompt="p",
                chat=fake_chat,
                verify=lambda w, a: VerifyResult(passed, ""),
                verifier_args={},
                cost_fn=lambda p, c: 0.0,
                norms_opts={"target_files": ["f.py"], "lint": False},
            )
        )

    ok = run(True)
    assert ok.norms is not None and ok.norms["files_changed"] == 0
    assert ok.norms_score == 1.0
    assert run(False).norms is None


def test_run_agent_cell_cleans_workdir_when_fixture_copy_fails(tmp_path, monkeypatch):
    import shutil
    import tempfile
    from pathlib import Path

    (tmp_path / "f.py").write_text("x = 1\n")
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

    async def fake_chat(**kwargs):
        raise AssertionError("chat must not run when setup fails")

    cell = asyncio.run(
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
        )
    )
    assert cell.outcome == "fail"
    assert "[harness error]" in cell.attempts[0].feedback
    assert created and all(not p.exists() for p in created)
