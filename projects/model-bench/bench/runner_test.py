import asyncio
from pathlib import Path

import pytest  # noqa: F401

from bench.runner import extract_files, run_cell
from bench.verifiers import VerifyResult


def test_extract_strips_fence_after_file_header():
    text = "FILE clusterrole.yaml\n```yaml\nrules:\n- verbs: [get, list]\n```"
    assert extract_files(text, ["clusterrole.yaml"]) == {
        "clusterrole.yaml": "rules:\n- verbs: [get, list]"
    }


def test_extract_strips_bare_fence_single_target():
    assert extract_files("```yaml\nfoo: 1\n```", ["values.yaml"]) == {
        "values.yaml": "foo: 1"
    }


def test_extract_strips_fence_with_surrounding_prose():
    text = "Here is the fixed file:\n```yaml\nfoo: 1\n```\nThat adds the key."
    assert extract_files(text, ["values.yaml"]) == {"values.yaml": "foo: 1"}


def test_extract_unfenced_passthrough():
    assert extract_files("foo: 1\nbar: 2", ["values.yaml"]) == {
        "values.yaml": "foo: 1\nbar: 2"
    }


def make_model(script):  # script: list of outputs per attempt
    calls = {"i": 0}

    async def complete(**kwargs):
        from bench.openrouter import Completion

        out = script[calls["i"]]
        calls["i"] += 1
        return Completion(text=out, prompt_tokens=10, completion_tokens=5, latency_ms=7)

    return complete


def test_pass_at_1_when_first_attempt_verifies(tmp_path):
    def verifier(workdir, args):
        return VerifyResult(True, "")

    cell = asyncio.run(
        run_cell(
            task_id="t",
            task_version="v1",
            model_id="m",
            content_hash="h",
            fixture_dir=tmp_path,
            target_files=["out.txt"],
            prompt="p",
            complete=make_model(["FILE out.txt\nok"]),
            verify=verifier,
            verifier_args={},
            cost_fn=lambda p, c: 0.001,
        )
    )
    assert cell.outcome == "pass@1" and len(cell.attempts) == 1


def test_pass_at_2_feeds_stderr_not_golden(tmp_path):
    def verifier(workdir, args):
        content = (Path(workdir) / "out.txt").read_text()
        return VerifyResult(
            content == "good", "" if content == "good" else "boom-stderr"
        )

    async def complete(**kwargs):
        from bench.openrouter import Completion

        msgs = kwargs["messages"]
        # second call must contain the verifier feedback, never a golden answer
        if any("boom-stderr" in m["content"] for m in msgs):
            text = "FILE out.txt\ngood"
        else:
            text = "FILE out.txt\nbad"
        return Completion(text=text, prompt_tokens=1, completion_tokens=1, latency_ms=1)

    cell = asyncio.run(
        run_cell(
            task_id="t",
            task_version="v1",
            model_id="m",
            content_hash="h",
            fixture_dir=tmp_path,
            target_files=["out.txt"],
            prompt="p",
            complete=complete,
            verify=verifier,
            verifier_args={},
            cost_fn=lambda p, c: 0.0,
        )
    )
    assert cell.outcome == "pass@2" and len(cell.attempts) == 2
