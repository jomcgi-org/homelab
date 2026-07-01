import os

import pytest  # noqa: F401

from bench.verifiers.sandbox import run_sandboxed, SandboxResult


def test_scrubs_cluster_and_token_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/secret/kubeconfig")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    monkeypatch.setenv("PATH", os.environ["PATH"])  # keep PATH so tools resolve
    res = run_sandboxed(["/usr/bin/env"], cwd=tmp_path, timeout_s=10)
    assert "KUBECONFIG" not in res.stdout
    assert "sk-secret" not in res.stdout


def test_returns_rc_and_streams(tmp_path):
    res = run_sandboxed(
        ["sh", "-c", "echo out; echo err 1>&2; exit 3"], cwd=tmp_path, timeout_s=10
    )
    assert res.rc == 3 and "out" in res.stdout and "err" in res.stderr


def test_timeout_is_nonzero_rc(tmp_path):
    res = run_sandboxed(["sh", "-c", "sleep 5"], cwd=tmp_path, timeout_s=1)
    assert res.rc != 0 and res.timed_out is True


def test_extra_env_is_merged_into_child(tmp_path):
    res = run_sandboxed(
        ["sh", "-c", "echo $PYTHONPATH"],
        cwd=tmp_path,
        timeout_s=10,
        extra_env={"PYTHONPATH": "."},
    )
    assert res.stdout.strip() == "."


def test_extra_env_cannot_smuggle_a_denied_key(tmp_path):
    # A task-authored overlay is still filtered through the deny-prefix guard, so it
    # cannot reintroduce a credential-shaped variable the scrub removed.
    res = run_sandboxed(
        ["sh", "-c", "echo ${OPENROUTER_API_KEY:-absent}"],
        cwd=tmp_path,
        timeout_s=10,
        extra_env={"OPENROUTER_API_KEY": "sk-leak"},
    )
    assert res.stdout.strip() == "absent"
