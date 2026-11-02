"""Run real Git pre-push hooks against disposable local bare remotes."""

import json
import os
import subprocess
import threading

import pytest

import push_format
import shim


def run(root, *args, ok=True):
    result = subprocess.run(args, cwd=root, capture_output=True, text=True)
    if ok:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def commit(root):
    run(root, "git", "add", "-A")
    run(root, "git", "commit", "-m", "fixture")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    remote = tmp_path / "remote.git"
    run(tmp_path, "git", "init", "--bare", str(remote))
    run(root, "git", "init", "-b", "main")
    run(root, "git", "config", "user.email", "fixture@example.test")
    run(root, "git", "config", "user.name", "Fixture")
    run(root, "git", "remote", "add", "origin", str(remote))
    (root / "bazel/tools/ci").mkdir(parents=True)
    # Production invokes bash, not sh. Every branch records entry and exits
    # explicitly so a failed assertion cannot accidentally test shell setup.
    (root / "bazel/tools/ci/ci").write_text("""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == validation-protocol ]]; then
  echo factory-push-format-v1
  exit 0
fi
echo "$1" >> "$FORMAT_TEST_TRACE"
case "$1" in
lint)
  if [[ -f missing ]]; then command -v deliberately_absent_formatter; fi
  if [[ -f lint_error ]]; then exit 17; fi
  if [[ -f source.txt ]]; then sed -i 's/BAD/GOOD/g' source.txt; fi
  ;;
regen)
  if [[ -f regen_error ]]; then exit 18; fi
  if [[ -f generate ]]; then echo generated > generated.txt; fi
  ;;
esac
exit 0
""")
    (root / "source.txt").write_text("GOOD\n")
    commit(root)
    run(root, "git", "push", "origin", "main")
    run(root, "git", "checkout", "-b", "factory/test")
    trace = tmp_path / "trace"
    os.environ["FORMAT_TEST_TRACE"] = str(trace)
    contract = {
        "task_id": "t-test",
        "node_key": "implement_test",
        "branch": "factory/test",
        "base_ref": "refs/remotes/origin/main",
    }
    state = push_format.install(root, contract)
    return root, remote, state, trace


def push(root):
    return run(root, "git", "push", "origin", "HEAD:refs/heads/factory/test", ok=False)


@pytest.mark.parametrize(
    "branch", ["factory/test", "factory/test-node-one", "fix/adopted"]
)
def test_single_branch_hydration_fetches_baseline_and_validates_assigned_push(
    repo, tmp_path, branch
):
    root, remote, state, trace = repo
    assert push(root).returncode == 0
    clone = tmp_path / "single-branch"
    run(
        tmp_path,
        "git",
        "clone",
        "--branch",
        "factory/test",
        "--single-branch",
        str(remote),
        str(clone),
    )
    assert (
        run(
            clone, "git", "rev-parse", "--verify", "refs/remotes/origin/main", ok=False
        ).returncode
        != 0
    )
    # Fetching main without a refspec still does not create origin/main.
    run(clone, "git", "fetch", "origin", "main")
    assert (
        run(
            clone, "git", "rev-parse", "--verify", "refs/remotes/origin/main", ok=False
        ).returncode
        != 0
    )
    run(clone, "git", "config", "user.email", "fixture@example.test")
    run(clone, "git", "config", "user.name", "Fixture")
    contract = json.loads((state / "contract.json").read_text())
    contract["branch"] = branch
    clone_state = push_format.install(clone, contract)
    (clone / "source.txt").write_text("BAD\n")
    commit(clone)
    refused = run(clone, "git", "push", "origin", "HEAD:refs/heads/" + branch, ok=False)
    assert refused.returncode != 0 and '"source.txt"' in refused.stderr
    assert push_format.evidence(clone_state)["file_count"] == 1
    (clone / "source.txt").write_text("GOOD\n")
    commit(clone)
    run(clone, "git", "push", "origin", "HEAD:refs/heads/" + branch)
    assert (
        run(clone, "git", "ls-remote", "origin", "refs/heads/" + branch).stdout.split()[
            0
        ]
        == run(clone, "git", "rev-parse", "HEAD").stdout.strip()
    )


def test_baseline_fetch_failure_is_actionable_and_not_drift(repo):
    root, remote, state, _ = repo
    run(remote, "git", "update-ref", "-d", "refs/heads/main")
    result = push(root)
    assert result.returncode != 0
    assert "cannot fetch push-validation baseline" in result.stderr
    assert push_format.evidence(state) is None


def test_committed_drift_then_correction_pushes_once(repo):
    root, remote, state, trace = repo
    (root / "source.txt").write_text("BAD\n")
    commit(root)
    assert run(root, "git", "status", "--porcelain").stdout == ""
    refused = push(root)
    assert refused.returncode != 0
    assert '"paths": ["source.txt"]' in refused.stderr
    assert trace.read_text().splitlines() == ["lint", "regen", "lint"]
    assert not run(root, "git", "ls-remote", "origin", "refs/heads/factory/test").stdout
    assert (root / "source.txt").read_text() == "BAD\n"
    # Count updates at the receiving repository, independently of hook output.
    received = remote / "received"
    post = remote / "hooks/post-receive"
    post.write_text(f'#!/bin/sh\ncat >> "{received}"\n')
    post.chmod(0o755)
    (root / "source.txt").write_text("GOOD\n")
    assert push(root).returncode != 0, (
        "uncommitted formatting must not push the stale commit"
    )
    assert not received.exists()
    commit(root)
    accepted = push(root)
    assert accepted.returncode == 0, accepted.stderr
    assert len(received.read_text().splitlines()) == 1
    assert push_format.evidence(state)["file_count"] == 1


@pytest.mark.parametrize("tracked", [False, True])
def test_regen_outputs_are_refused(repo, tracked):
    root, _, state, trace = repo
    if tracked:
        (root / "generated.txt").write_text("old\n")
    (root / "generate").touch()
    commit(root)
    result = push(root)
    assert result.returncode != 0
    assert '"paths": ["generated.txt"]' in result.stderr
    assert "regen" in trace.read_text()
    assert push_format.evidence(state)["paths"] == ["generated.txt"]


@pytest.mark.parametrize(
    "failure,entered",
    [
        ("missing", ["lint"]),
        ("lint_error", ["lint"]),
        ("regen_error", ["lint", "regen"]),
    ],
)
def test_errors_fail_without_counting_drift(repo, failure, entered):
    root, _, state, trace = repo
    (root / failure).touch()
    commit(root)
    result = push(root)
    assert result.returncode != 0
    assert "ci " in result.stderr and " exited " in result.stderr
    assert trace.read_text().splitlines() == entered
    assert push_format.evidence(state) is None
    assert not run(root, "git", "ls-remote", "origin", "refs/heads/factory/test").stdout


def test_linked_worktree_artifacts_unrelated_changes_and_existing_hook(repo, tmp_path):
    root, _, state, trace = repo
    original = root / ".git/hooks/pre-push"
    old_trace = tmp_path / "old-hook"
    original.write_text(f'#!/bin/sh\ncat >> "{old_trace}"\nexit 0\n')
    original.chmod(0o755)
    linked = tmp_path / "linked"
    run(root, "git", "worktree", "add", "--detach", str(linked), "HEAD")
    artifact = root / ".factory/result.json"
    artifact.parent.mkdir()
    artifact.write_text('{"transient": true}')
    (linked / "unrelated.txt").write_text("keep this\n")
    (root / "source.txt").write_text("unrelated dirty worktree\n")
    result = push(linked)
    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == ["lint", "regen", "lint"]
    assert "refs/heads/factory/test" in old_trace.read_text()
    assert artifact.read_text() == '{"transient": true}'
    assert (root / "source.txt").read_text() == "unrelated dirty worktree\n"
    assert (linked / "unrelated.txt").read_text() == "keep this\n"
    assert push_format.evidence(state) is None


def test_human_branch_preserves_existing_hook_without_validation(repo, tmp_path):
    root, _, _, trace = repo
    original = root / ".git/hooks/pre-push"
    old_trace = tmp_path / "human-hook"
    original.write_text(f'#!/bin/sh\ncat > "{old_trace}"\nexit 23\n')
    original.chmod(0o755)
    result = run(root, "git", "push", "origin", "HEAD:refs/heads/human", ok=False)
    assert result.returncode != 0
    assert "refs/heads/human" in old_trace.read_text()
    assert not trace.exists()


def test_duplicate_drift_counts_distinct_paths(repo):
    root, _, state, _ = repo
    (root / "source.txt").write_text("BAD\n")
    commit(root)
    assert push(root).returncode != 0
    assert push(root).returncode != 0
    assert push_format.evidence(state)["file_count"] == 1


def test_wrong_factory_target_is_refused(repo):
    root, _, _, trace = repo
    result = run(
        root, "git", "push", "origin", "HEAD:refs/heads/factory/other", ok=False
    )
    assert result.returncode != 0
    assert "differs from the assigned branch" in result.stderr
    assert not trace.exists()
    assert not run(
        root, "git", "ls-remote", "origin", "refs/heads/factory/other"
    ).stdout


def test_old_helper_cannot_silently_ignore_complete_validation(repo):
    root, _, state, trace = repo
    helper = root / "bazel/tools/ci/ci"
    helper.write_text(
        '#!/bin/bash\necho "old helper: BUILD generation deferred to CI"\nexit 0\n'
    )
    commit(root)
    result = push(root)
    assert result.returncode != 0
    assert "lacks complete validation protocol v1" in result.stderr
    assert not trace.exists()
    assert push_format.evidence(state) is None
    assert not run(root, "git", "ls-remote", "origin", "refs/heads/factory/test").stdout


def test_relative_custom_hooks_receive_stdin_and_other_hooks_survive(repo, tmp_path):
    root, _, _, _ = repo
    # Simulate an existing hooksPath before installing into a fresh Git state.
    custom = root / ".custom-hooks"
    custom.mkdir()
    trace = tmp_path / "custom"
    for name in ("pre-push", "post-commit"):
        script = custom / name
        script.write_text(
            f'#!/bin/sh\nprintf "{name}\\n" >> "{trace}"\n'
            + (f'cat >> "{trace}"\n' if name == "pre-push" else "")
        )
        script.chmod(0o755)
    state = root / ".git/factory-push-format"
    (state / "original-hooks").write_text(".custom-hooks")
    push_format.install(root, json.loads((state / "contract.json").read_text()))
    (root / "source.txt").write_text("GOOD changed\n")
    commit(root)
    assert push(root).returncode == 0
    assert "post-commit" in trace.read_text()
    assert "pre-push" in trace.read_text()
    assert "refs/heads/factory/test" in trace.read_text()


def test_real_turn_installs_guard_and_returns_evidence_after_worker_success(
    repo, monkeypatch
):
    root, _, state, trace = repo
    contract = json.loads((state / "contract.json").read_text())
    # Remove only the local selector to prove ProcessManager.turn installs it.
    run(root, "git", "config", "--unset", "core.hooksPath")
    (root / "source.txt").write_text("BAD\n")
    commit(root)

    class Adapter:
        workspace = str(root)

        def turn(self, *args, **kwargs):
            assert push(root).returncode != 0
            (root / "source.txt").write_text("GOOD\n")
            commit(root)
            assert push(root).returncode == 0
            return {"result": "worker succeeded", "usage": {}}

    manager = object.__new__(shim.ProcessManager)
    manager.workspace = str(root.parent)
    manager.claude = Adapter()
    manager.codex = manager.pi = manager.muse = object()
    manager._mount_lock = threading.Lock()
    manager._adapter = lambda model: manager.claude
    monkeypatch.setattr(shim, "ensure_workspace_volume", lambda: None)
    monkeypatch.setattr(shim, "apply_egress_ca_trust", lambda: None)
    monkeypatch.setattr(shim, "_sync_session_volume", lambda: None)
    monkeypatch.setattr(shim, "_cli_privilege_kwargs", lambda: {})
    result = manager.turn("deliver", push_format=contract)
    assert result["result"] == "worker succeeded"
    assert result["usage"]["push_format"]["paths"] == ["source.txt"]
    assert result["usage"]["push_format"]["node_key"] == "implement_test"
    assert trace.read_text().splitlines() == ["lint", "regen", "lint"] * 2
