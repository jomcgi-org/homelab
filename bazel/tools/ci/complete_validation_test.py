"""Exercise the actual ci helper, including committed input enumeration."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.test"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    helper = root / "bazel/tools/ci/ci"
    helper.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).with_name("ci"), helper)
    (root / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=root, check=True
    )
    (root / "a.py").write_text("x=2\n")
    subprocess.run(["git", "commit", "-qam", "change"], cwd=root, check=True)
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("ruff", "shfmt", "gofumpt", "prettier", "buildifier"):
        script = tools / tool
        script.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$FORMAT_TRACE"\nexit 0\n'
        )
        script.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(tools) + ":" + os.environ["PATH"],
        CI_REQUIRE_COMPLETE="1",
        FORMAT_TRACE=str(tmp_path / "trace"),
        XDG_CACHE_HOME=str(tmp_path / "empty-cache"),
    )
    return root, helper, tools, env


def invoke(checkout, command):
    root, helper, _, env = checkout
    return subprocess.run(
        ["bash", str(helper), command],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )


def test_committed_inputs_reach_formatter_without_process_substitution(checkout):
    result = invoke(checkout, "lint")
    assert result.returncode == 0, result.stderr
    assert "ruff format a.py" in Path(checkout[3]["FORMAT_TRACE"]).read_text()
    assert "/dev/fd" not in result.stderr


def test_complete_validation_protocol_is_explicit(checkout):
    result = invoke(checkout, "validation-protocol")
    assert result.returncode == 0
    assert result.stdout.strip() == "factory-push-format-v1"


def test_nonzero_buildifier_refuses_complete_validation(checkout):
    # A failing executable is also incomplete; it must reach the BUILD category.
    root, _, tools, _ = checkout
    (root / "BUILD").write_text("# changed\n")
    (tools / "buildifier").write_text("#!/bin/sh\nexit 19\n")
    result = invoke(checkout, "lint")
    assert result.returncode == 19


def test_missing_required_tool_stops_before_formatting(checkout):
    _, _, tools, env = checkout
    (tools / "buildifier").unlink()
    for tool in ("bash", "git", "sed", "sort", "tr"):
        (tools / tool).symlink_to(shutil.which(tool))
    env["PATH"] = str(tools)
    result = invoke(checkout, "lint")
    assert result.returncode != 0
    assert "missing tool: buildifier" in result.stderr
    assert not Path(env["FORMAT_TRACE"]).exists()


def test_git_quoted_names_are_not_silently_skipped(checkout):
    root, _, _, env = checkout
    name = 'unicode-\u00e9-"tab\t.py'
    (root / name).write_text("x=1\n")
    subprocess.run(["git", "add", name], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "quoted path"], cwd=root, check=True)
    result = invoke(checkout, "lint")
    assert result.returncode == 0, result.stderr
    assert name in Path(env["FORMAT_TRACE"]).read_text()


def test_missing_pinned_generator_is_not_reported_as_regen_success(checkout):
    result = invoke(checkout, "regen")
    assert result.returncode != 0
    assert "pinned Linux //:gazelle_binary" in result.stderr
    assert "regen: done" not in result.stdout


def test_complete_regen_passes_bzlmod_config_and_goroot_on_both_passes(
    checkout, tmp_path
):
    root, helper, tools, env = checkout
    bundle = tmp_path / "libexec"
    bundle.mkdir()
    # Substitute only the fixture's installed prefix; production has no bypass.
    helper.write_text(helper.read_text().replace("/usr/local/libexec", str(bundle)))
    (bundle / "gazelle_inputs.py").write_text("pass\n")
    (bundle / "gazelle-repo-config").write_text("# fixture mapping\n")
    generator = bundle / "homelab-gazelle"
    generator.write_text(
        '#!/bin/sh\nprintf "%s %s\\n" "$GOROOT" "$*" >> "$FORMAT_TRACE"\n'
    )
    generator.chmod(0o755)
    go = tools / "go"
    go.write_text("#!/bin/sh\necho /pinned/go\n")
    go.chmod(0o755)
    result = invoke(checkout, "regen")
    assert result.returncode == 0, result.stdout + result.stderr
    commands = Path(env["FORMAT_TRACE"]).read_text().splitlines()
    assert len(commands) == 2
    assert all(command.startswith("/pinned/go ") for command in commands)
    assert all(
        f"-bzlmod -repo_config {bundle}/gazelle-repo-config -external=static" in command
        for command in commands
    )
    assert commands[1].endswith("-lang=go,proto")


@pytest.mark.parametrize("step", ["lint", "regen"])
def test_missing_baseline_fails_before_a_no_changes_claim(checkout, step):
    root, _, _, _ = checkout
    subprocess.run(
        ["git", "update-ref", "-d", "refs/remotes/origin/main"], cwd=root, check=True
    )
    result = invoke(checkout, step)
    assert result.returncode != 0
    assert "done" not in result.stdout and "no changed files" not in result.stdout
