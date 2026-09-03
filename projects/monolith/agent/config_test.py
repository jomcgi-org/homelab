# projects/monolith/agent/config_test.py
import pytest

from agent.config import (
    agent_sessions_channel_notify,
    load_drainer_settings,
    load_settings,
)
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
        "DRAINER_JOB_KINDS",
        "DRAINER_JOB_KIND",
        "DRAINER_KG_MAX_JOBS_PER_DAY",
        "DRAINER_DOCFIX_AUTO_MERGE",
        "KG_DOCFIX_REVIEW_ENABLED",
        "DRAINER_REPO",
        "DRAINER_BRANCH",
        "DRAINER_REASONING",
        "DRAINER_NOTIFY_FAILURES",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_drainer_settings()

    assert settings.enabled is False
    assert settings.max_jobs_per_cycle == 3
    assert settings.turn_timeout_seconds == 1800
    assert settings.stall_threshold_seconds == 2700
    assert settings.job_kinds == ("qwen-drain", "kg-drain")
    assert settings.kg_max_jobs_per_day == 40
    assert settings.docfix_auto_merge is False
    assert settings.docfix_review_enabled is False
    assert settings.repo == GITHUB_REPO
    assert settings.repo in REPO_CATALOG
    assert settings.branch == "main"
    # Luna drain jobs are usually multi-step repo audits, so high reasoning is
    # the lane default while payloads can still opt out.
    assert settings.reasoning is True
    assert settings.notify_failures is False


def test_drainer_environment_overrides(monkeypatch):
    monkeypatch.setenv("DRAINER_ENABLED", "true")
    monkeypatch.setenv("DRAINER_MAX_JOBS_PER_CYCLE", "5")
    monkeypatch.setenv("DRAINER_TURN_TIMEOUT_SECONDS", "42")
    monkeypatch.setenv("DRAINER_STALL_THRESHOLD_SECONDS", "84")
    monkeypatch.setenv("DRAINER_JOB_KINDS", "custom-drain, kg-drain")
    monkeypatch.setenv("DRAINER_JOB_KIND", "legacy-ignored")
    monkeypatch.setenv("DRAINER_KG_MAX_JOBS_PER_DAY", "12")
    monkeypatch.setenv("DRAINER_DOCFIX_AUTO_MERGE", "true")
    monkeypatch.setenv("KG_DOCFIX_REVIEW_ENABLED", "true")
    monkeypatch.setenv("DRAINER_REPO", "weave-hand/loom")
    monkeypatch.setenv("DRAINER_BRANCH", "work")
    monkeypatch.setenv("DRAINER_REASONING", "false")
    monkeypatch.setenv("DRAINER_NOTIFY_FAILURES", "true")

    settings = load_drainer_settings()

    assert settings.reasoning is False

    assert settings.enabled is True
    assert settings.max_jobs_per_cycle == 5
    assert settings.turn_timeout_seconds == 42
    assert settings.stall_threshold_seconds == 84
    assert settings.job_kinds == ("custom-drain", "kg-drain")
    assert settings.kg_max_jobs_per_day == 12
    assert settings.docfix_auto_merge is True
    assert settings.docfix_review_enabled is True
    assert settings.repo == "weave-hand/loom"
    assert settings.branch == "work"
    assert settings.notify_failures is True


def test_agent_sessions_channel_notify_defaults_to_needs_input(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_CHANNEL_NOTIFY", raising=False)

    assert agent_sessions_channel_notify() == "needs-input"


@pytest.mark.parametrize("value", ["needs-input", "all", "none"])
def test_agent_sessions_channel_notify_parses_supported_values(monkeypatch, value):
    monkeypatch.setenv("AGENT_SESSIONS_CHANNEL_NOTIFY", value.upper())

    assert agent_sessions_channel_notify() == value


def test_agent_sessions_channel_notify_rejects_invalid_value(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_CHANNEL_NOTIFY", "warnings")

    with pytest.raises(ValueError, match="must be one of"):
        agent_sessions_channel_notify()


def test_drainer_notify_failures_rejects_invalid_boolean(monkeypatch):
    monkeypatch.setenv("DRAINER_NOTIFY_FAILURES", "sometimes")

    with pytest.raises(ValueError, match="must be true or false"):
        load_drainer_settings()


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
def test_drainer_notify_failures_parses_boolean(monkeypatch, value, expected):
    monkeypatch.setenv("DRAINER_NOTIFY_FAILURES", value)

    assert load_drainer_settings().notify_failures is expected


def test_drainer_legacy_kind_fallback(monkeypatch):
    monkeypatch.delenv("DRAINER_JOB_KINDS", raising=False)
    monkeypatch.setenv("DRAINER_JOB_KIND", "legacy-drain,with-comma")

    assert load_drainer_settings().job_kinds == ("legacy-drain,with-comma",)


def test_drainer_empty_kinds_pause_all_claims(monkeypatch):
    monkeypatch.setenv("DRAINER_JOB_KINDS", "")
    monkeypatch.setenv("DRAINER_JOB_KIND", "legacy-must-not-win")

    assert load_drainer_settings().job_kinds == ()
