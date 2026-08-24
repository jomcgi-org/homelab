"""Tests for shared GitHub repository configuration."""

import importlib


def _read_github_repo() -> str:
    import core.github as github

    return importlib.reload(github).GITHUB_REPO


def test_github_repo_defaults_to_current_slug(monkeypatch):
    monkeypatch.delenv("GITHUB_REPO", raising=False)

    assert _read_github_repo() == "jomcgi-org/homelab"


def test_github_repo_environment_override(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "example/alternate-repo")

    assert _read_github_repo() == "example/alternate-repo"
