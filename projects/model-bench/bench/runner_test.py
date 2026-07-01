import asyncio
from pathlib import Path

import pytest  # noqa: F401

from bench.runner import _augment_prompt_with_files, extract_files, run_cell
from bench.verifiers import VerifyResult


def test_extract_takes_first_fenced_block_not_last():
    # A file block followed by a trailing example block: only the file is taken.
    text = "FILE values.yaml\n```yaml\nfoo: 1\n```\nExample:\n```bash\nyq -i x\n```"
    assert extract_files(text, ["values.yaml"]) == {"values.yaml": "foo: 1"}


def test_augment_prompt_injects_file_contents(tmp_path):
    (tmp_path / "values.yaml").write_text('image:\n  tag: ""\n')
    out = _augment_prompt_with_files("set the tag", tmp_path, ["values.yaml"])
    assert "set the tag" in out
    assert "FILE values.yaml" in out and "image:" in out


def test_augment_prompt_noop_for_free_text(tmp_path):
    assert (
        _augment_prompt_with_files("write a commit", tmp_path, []) == "write a commit"
    )


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


def test_extract_single_target_ignores_mismatched_file_path():
    # Model wrote `FILE values.yaml` but the target is `chart/values.yaml`. The
    # content must still map to the target, not be dropped (which would grade the
    # untouched fixture). This is the helm shot-1 failure regression.
    text = "FILE values.yaml\nimage:\n  repository: myrepo/demo\n  tag: 1.4.2"
    assert extract_files(text, ["chart/values.yaml"]) == {
        "chart/values.yaml": "image:\n  repository: myrepo/demo\n  tag: 1.4.2"
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


def test_parse_structured_json_beats_text(tmp_path):
    from bench.runner import _parse_structured_or_extract

    # Clean JSON content maps path-agnostically to the single target.
    got = _parse_structured_or_extract(
        '{"files": [{"path": "x.py", "content": "def f():\\n    return 1"}]}',
        ["extract_text.py"],
    )
    assert got == {"extract_text.py": "def f():\n    return 1"}
    # Fenced JSON still parses.
    got2 = _parse_structured_or_extract(
        '```json\n{"files": [{"path": "a", "content": "ok"}]}\n```', ["a"]
    )
    assert got2 == {"a": "ok"}
    # Non-JSON falls back to lenient text extraction.
    assert _parse_structured_or_extract("def f(): pass", ["a.py"]) == {
        "a.py": "def f(): pass"
    }
