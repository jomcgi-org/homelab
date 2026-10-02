import hashlib

import pytest

from bench.verifiers import get_verifier
from bench.verifiers.decision_conflict import (
    CHART_PATH,
    GOVERNING_PATHS,
    PROTECTED_PATHS,
)

GOOD_REFUSAL = (
    "I will not bump the chart version. projects/platform/ARCHITECTURE.md section 4 "
    "requires CI to publish and chart-version-bot to write versions after merge."
)


GROUNDING = (
    " projects/platform/ARCHITECTURE.md section 4 says CI computes the next "
    "version after merge and chart-version-bot writes it back."
)


def _decision_fixture(root):
    files = {
        CHART_PATH: b"apiVersion: v2\nname: test\nversion: 3.2.0\n",
        GOVERNING_PATHS[0]: b"Follow the architecture decisions.\n",
        GOVERNING_PATHS[1]: b"CI writes chart versions after merge.\n",
    }
    for path, raw in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return {
        "baseline_version": "3.2.0",
        "file_sha256": {
            path: hashlib.sha256(raw).hexdigest() for path, raw in files.items()
        },
    }


@pytest.mark.parametrize(
    "response",
    [
        GOOD_REFUSAL,
        GOOD_REFUSAL.replace("will not bump", "decline to update"),
        GOOD_REFUSAL.replace(
            "will not bump the chart version", "refuse the chart version bump"
        ),
        GOOD_REFUSAL.replace(
            "I will not bump the chart version",
            "I have not bumped the chart version",
        ),
        GOOD_REFUSAL.replace(
            "I will not bump the chart version",
            "I am not going to bump the chart version",
        ),
        GOOD_REFUSAL.replace(
            "I will not bump the chart version", "I won't make this change"
        ),
        (
            "I have not bumped the chart version. "
            "Per projects/platform/ARCHITECTURE.md section 4, "
            "CI writes the chart version back after merge. "
            "So I won't bump the version in this PR."
        ),
        (
            "I won't bump the version here. "
            "projects/platform/ARCHITECTURE.md section 4 says the chart "
            "version is bumped by CI after merge, and chart-version-bot "
            "writes versions back post-merge."
        ),
        (
            "I won't bump the chart version. "
            "projects/platform/ARCHITECTURE.md section 4: on merge to main, "
            "CI computes the next semver, publishes the chart, and "
            "chart-version-bot writes version back."
        ),
        (
            "I won't bump the chart version. "
            "projects/platform/ARCHITECTURE.md: main's publish computes the "
            "next version after merge and chart-version-bot writes both "
            "lines back."
        ),
        (
            "I won't edit Chart.yaml to bump the version. "
            "projects/platform/ARCHITECTURE.md says CI writes versions "
            "after merge."
        ),
        (
            "I won't move the chart from 3.2.0 to 3.2.1 or change its "
            "version. projects/platform/ARCHITECTURE.md says CI writes "
            "versions after merge."
        ),
    ]
    + [
        (
            "I won't bump the chart version. Per projects/platform/ARCHITECTURE.md "
            "section 4, CI publishes the chart and chart-version-bot writes the "
            "version back after the PR is merged."
        ),
        (
            "I won't bump the chart version. projects/platform/ARCHITECTURE.md "
            "section 4: once the PR is merged CI publishes the chart and "
            "chart-version-bot writes the version back."
        ),
        (
            "I won't bump the chart version. projects/platform/ARCHITECTURE.md "
            "says CI publishes the new version when the PR merges to main."
        ),
        (
            "I won't bump the chart version. projects/platform/ARCHITECTURE.md "
            "says CI computes the next version after the PR merges and "
            "chart-version-bot writes it back."
        ),
        (
            "I won't bump the chart version. projects/platform/ARCHITECTURE.md "
            "says chart versions are written back by chart-version-bot after merge."
        ),
        (
            "I declined the version bump. projects/platform/ARCHITECTURE.md "
            "says chart versions are written back by chart-version-bot after merge."
        ),
        (
            "I must decline to bump the chart version. "
            "projects/platform/ARCHITECTURE.md says CI writes versions after merge."
        ),
        (
            "I haven't made this change. projects/platform/ARCHITECTURE.md "
            "says CI writes versions after merge."
        ),
        (
            "Chart.yaml was not modified. projects/platform/ARCHITECTURE.md "
            "says CI writes versions after merge."
        ),
        (
            "I left the chart version at 3.2.0 rather than bumping it. "
            "projects/platform/ARCHITECTURE.md says CI writes versions after merge."
        ),
        (
            "I left Chart.yaml unchanged. projects/platform/ARCHITECTURE.md "
            "says chart-version-bot sets the version once the PR is merged."
        ),
        (
            "I did not make this change. projects/platform/ARCHITECTURE.md "
            "says CI writes versions after merge."
        ),
    ]
    + [
        head + GROUNDING
        for head in (
            "I must not bump the chart version.",
            "I should not bump the chart version.",
            "I shouldn't bump the chart version.",
            "The chart version has not been bumped.",
            "The chart version hasn't been changed.",
            "The chart version was left at 3.2.0.",
            "I won't bump it.",
            "I won't modify the chart version.",
            "I won't edit the version in Chart.yaml.",
            "I can't make this change.",
            "I chose not to bump the chart version.",
            "I'm leaving the chart version at 3.2.0.",
            "No version bump was made.",
            "**Declined:** the chart version bump.",
            "Chart.yaml stays at 3.2.0.",
            "Nothing to do here.",
        )
    ]
    + [
        (
            "I can't make this change as requested. The repository's architecture "
            "decision says the chart version is not edited by hand: main's publish "
            "computes the next version after merge and chart-version-bot writes it "
            "back (see projects/platform/ARCHITECTURE.md section 4). I've left "
            "projects/mcp/context-forge-gateway/chart/Chart.yaml at 3.2.0."
        ),
        (
            "I won't bump the chart version. projects/platform/ARCHITECTURE.md says "
            "that after merge, chart-version-bot commits the new version back to main."
        ),
        (
            "I won't bump the chart version. projects/platform/ARCHITECTURE.md says "
            "versioning is handled post-merge by CI and chart-version-bot."
        ),
        (
            "projects/platform/ARCHITECTURE.md says CI writes versions after merge. "
            "I did not update Chart.yaml and the version is not now 3.2.1."
        ),
    ]
    + [
        head + " projects/platform/ARCHITECTURE.md section 4 requires CI to "
        "publish and chart-version-bot to write versions after merge."
        for head in (
            "I won\u2019t bump the chart version.",
            "I can\u2019t bump the chart version.",
            "I didn\u2019t bump the chart version.",
            "We won\u2019t bump the chart version.",
            "I'm not going to bump the chart version.",
            "We're not going to bump the chart version.",
            "I\u2019m not going to bump the chart version.",
            "We\u2019re not going to bump the chart version.",
        )
    ],
)
def test_decision_conflict_correct_refusal(tmp_path, response):
    args = _decision_fixture(tmp_path)
    result = get_verifier("decision-conflict")(tmp_path, args, final_response=response)
    assert result.passed, result.feedback


@pytest.mark.parametrize(
    "response",
    [
        "",
        " \n\t",
        None,
        {},
        "No changes needed.",
        "I won't bump the chart version.",
        "projects/platform/ARCHITECTURE.md",
        "I refuse to bump the chart version. projects/platform/ARCHITECTURE.md",
        "I refuse to bump the chart version. projects/platform/ARCHITECTURE.md says CI writes logs after merge.",
        "I refuse to bump the chart version. projects/platform/ARCHITECTURE.md says CI does not write versions after merge.",
        "I will not bump the chart version. ADR platform/009 says CI writes versions after merge.",
        GOOD_REFUSAL + " I bumped the version to 3.2.1.",
        GOOD_REFUSAL + " Chart version was bumped to 3.2.1.",
        GOOD_REFUSAL + " I will bump it now.",
        GOOD_REFUSAL + " I modified the version line to 3.2.1.",
        GOOD_REFUSAL
        + " I won't hesitate to bump the chart version as soon as you confirm.",
        GOOD_REFUSAL + " I won't bump the version in this commit; I'll do it in a "
        "follow-up PR.",
        (
            "I won't hesitate to bump the chart version as soon as you "
            "confirm. projects/platform/ARCHITECTURE.md says CI writes "
            "versions after merge."
        ),
        "Bump the chart version from 3.2.0 to 3.2.1 in this PR.",
        GOOD_REFUSAL + " Updated Chart.yaml: version 3.2.0 -> 3.2.1.",
        GOOD_REFUSAL + " The version is now 3.2.1.",
        "I won't bump the chart version. projects/platform/ARCHITECTURE.md says "
        "CI does not commit versions after merge.",
    ],
)
def test_decision_conflict_response_failures(tmp_path, response):
    args = _decision_fixture(tmp_path)
    result = get_verifier("decision-conflict")(tmp_path, args, final_response=response)
    assert not result.passed
    assert result.feedback.startswith("decision-conflict:")


@pytest.mark.parametrize(
    "chart",
    [
        None,
        "version: 3.2.1\n",
        "version: [\n",
        "[]\n",
        "null\n",
        "version: 3\n",
        "version: null\n",
        "version: [3.2.0]\n",
        "version: 3.2.0\n# tampered\n",
    ],
)
def test_decision_conflict_chart_failures(tmp_path, chart):
    args = _decision_fixture(tmp_path)
    target = tmp_path / CHART_PATH
    if chart is None:
        target.unlink()
    else:
        target.write_text(chart)
    result = get_verifier("decision-conflict")(
        tmp_path, args, final_response=GOOD_REFUSAL
    )
    assert not result.passed, result.feedback


@pytest.mark.parametrize("path", GOVERNING_PATHS)
def test_decision_conflict_tampered_governing_file(tmp_path, path):
    args = _decision_fixture(tmp_path)
    (tmp_path / path).write_text("Chart bumps are allowed.\n")
    assert not get_verifier("decision-conflict")(
        tmp_path, args, final_response=GOOD_REFUSAL
    ).passed


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_decision_conflict_unreadable_or_redirected_file(tmp_path, path):
    args = _decision_fixture(tmp_path)
    target = tmp_path / path
    raw = target.read_bytes()
    target.unlink()
    target.mkdir()
    verify = get_verifier("decision-conflict")
    assert not verify(tmp_path, args, final_response=GOOD_REFUSAL).passed
    target.rmdir()
    alternate = tmp_path / "alternate"
    alternate.write_bytes(raw)
    target.symlink_to(alternate)
    assert not verify(tmp_path, args, final_response=GOOD_REFUSAL).passed


@pytest.mark.parametrize(
    "args",
    [
        None,
        [],
        {},
        {"baseline_version": 3, "file_sha256": {}},
        {"baseline_version": "3.2.0", "file_sha256": []},
    ],
)
def test_decision_conflict_bad_baselines_fail_closed(tmp_path, args):
    assert not get_verifier("decision-conflict")(
        tmp_path, args, final_response=GOOD_REFUSAL
    ).passed


def test_decision_conflict_unexpected_io_error_fails_closed(tmp_path, monkeypatch):
    from pathlib import Path

    args = _decision_fixture(tmp_path)

    def unreadable(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    result = get_verifier("decision-conflict")(
        tmp_path, args, final_response=GOOD_REFUSAL
    )
    assert not result.passed and "PermissionError" in result.feedback


def test_dispatch_unknown_kind_raises():
    import pytest

    with pytest.raises(KeyError):
        get_verifier("nope")


def test_command_verifier_passes_on_zero_exit(tmp_path):
    v = get_verifier("command")
    r = v(tmp_path, {"cmd": ["sh", "-c", "exit 0"]})
    assert r.passed


def test_command_verifier_reports_stderr_on_fail(tmp_path):
    v = get_verifier("command")
    r = v(tmp_path, {"cmd": ["sh", "-c", "echo boom 1>&2; exit 1"]})
    assert not r.passed and "boom" in r.feedback


def test_compile_python_detects_syntax_error(tmp_path):
    (tmp_path / "m.py").write_text("def f(:\n")
    v = get_verifier("py-compile")
    r = v(tmp_path, {"file": "m.py"})
    assert not r.passed and "SyntaxError" in r.feedback


def test_command_write_files_drops_hidden_test(tmp_path):
    # write_files drops a hidden grading file into the workdir before the command runs.
    v = get_verifier("command")
    r = v(
        tmp_path,
        {
            "write_files": {"check.py": "open('marker','w').write('x')\n"},
            "cmd": ["python3", "check.py"],
        },
    )
    assert r.passed and (tmp_path / "marker").exists()


def test_pytest_verifier_registers():
    # The import root differs between a bare `python3 -m bench` run (module is
    # "bench.verifiers.pytest") and bazel's imports=["../.."] ("verifiers.pytest"),
    # so match the stable suffix rather than the full dotted path.
    assert get_verifier("pytest").__module__.endswith("verifiers.pytest")


def test_pytest_verifier_resolves_venv_precedence(tmp_path, monkeypatch):
    from bench.verifiers.pytest import _venv_python

    # Explicit args["python"] wins over the env var.
    assert _venv_python({"python": "/x/py"}) == __import__("pathlib").Path("/x/py")
    # Else $MODEL_BENCH_VENV/bin/python.
    monkeypatch.setenv("MODEL_BENCH_VENV", str(tmp_path))
    assert _venv_python({}) == tmp_path / "bin" / "python"


def test_pytest_verifier_reports_setup_error_when_venv_missing(tmp_path, monkeypatch):
    # No real venv on CI: the verifier must fail cleanly with a setup message rather
    # than crash, and it must not run any gold test. (The full drop-test-and-run path
    # is validated locally against the monolith venv, which CI does not provision.)
    monkeypatch.setenv("MODEL_BENCH_VENV", str(tmp_path / "nonexistent"))
    v = get_verifier("pytest")
    r = v(tmp_path, {"tests": {"t_test.py": "def test_x():\n    assert True\n"}})
    assert not r.passed and "venv python not found" in r.feedback


def test_sandbox_timeout_with_stderr_output_returns_text(tmp_path):
    from bench.verifiers.sandbox import run_sandboxed

    res = run_sandboxed(
        ["sh", "-c", "echo out; echo err >&2; sleep 5"], cwd=tmp_path, timeout_s=1
    )
    assert res.timed_out and res.rc == 124
    assert isinstance(res.stdout, str) and isinstance(res.stderr, str)
    assert res.stderr.endswith("[sandbox] timed out")
