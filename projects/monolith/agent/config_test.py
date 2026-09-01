# projects/monolith/agent/config_test.py
import pytest

from agent.config import load_drainer_settings, load_settings
from core.github import GITHUB_REPO
from goosecracker.api import REPO_CATALOG


def test_load_settings(monkeypatch):
    monkeypatch.setenv(
        "MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "1501965852042330302"
    )
    monkeypatch.setenv(
        "MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID", "1501965852969402517"
    )

    s = load_settings()

    assert s.discord_default_server_id == "1501965852042330302"
    assert s.discord_default_channel_id == "1501965852969402517"


def test_missing_required_env_raises(monkeypatch):
    monkeypatch.delenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", raising=False)
    monkeypatch.delenv("MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID", raising=False)

    with pytest.raises(KeyError):
        load_settings()


def test_drainer_defaults(monkeypatch):
    for name in (
        "DRAINER_ENABLED",
        "DRAINER_MAX_JOBS_PER_CYCLE",
        "DRAINER_TURN_TIMEOUT_SECONDS",
        "DRAINER_STALL_THRESHOLD_SECONDS",
        "DRAINER_JOB_KIND",
        "DRAINER_REPO",
        "DRAINER_BRANCH",
        "DRAINER_REASONING",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_drainer_settings()

    assert settings.enabled is False
    assert settings.max_jobs_per_cycle == 3
    assert settings.turn_timeout_seconds == 1800
    assert settings.stall_threshold_seconds == 2700
    assert settings.job_kind == "qwen-drain"
    assert settings.repo == GITHUB_REPO
    assert settings.repo in REPO_CATALOG
    assert settings.branch == "main"
    # Luna drain jobs are usually multi-step repo audits, so high reasoning is
    # the lane default while payloads can still opt out.
    assert settings.reasoning is True


def test_drainer_environment_overrides(monkeypatch):
    monkeypatch.setenv("DRAINER_ENABLED", "true")
    monkeypatch.setenv("DRAINER_MAX_JOBS_PER_CYCLE", "5")
    monkeypatch.setenv("DRAINER_TURN_TIMEOUT_SECONDS", "42")
    monkeypatch.setenv("DRAINER_STALL_THRESHOLD_SECONDS", "84")
    monkeypatch.setenv("DRAINER_JOB_KIND", "custom-drain")
    monkeypatch.setenv("DRAINER_REPO", "weave-hand/loom")
    monkeypatch.setenv("DRAINER_BRANCH", "work")
    monkeypatch.setenv("DRAINER_REASONING", "false")

    settings = load_drainer_settings()

    assert settings.reasoning is False

    assert settings.enabled is True
    assert settings.max_jobs_per_cycle == 5
    assert settings.turn_timeout_seconds == 42
    assert settings.stall_threshold_seconds == 84
    assert settings.job_kind == "custom-drain"
    assert settings.repo == "weave-hand/loom"
    assert settings.branch == "work"
