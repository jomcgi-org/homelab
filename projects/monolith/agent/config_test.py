# projects/monolith/agent/config_test.py
import pytest

from agent.config import load_settings


def test_load_settings_with_allow_list(monkeypatch):
    monkeypatch.setenv(
        "MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "1501965852042330302"
    )
    monkeypatch.setenv(
        "MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID", "1501965852969402517"
    )
    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_ALLOWED_CHANNEL_IDS", "9999, 8888")

    s = load_settings()

    assert s.discord_default_server_id == "1501965852042330302"
    assert s.discord_default_channel_id == "1501965852969402517"
    assert s.discord_allowed_channel_ids == frozenset(
        {"1501965852969402517", "9999", "8888"}
    )


def test_default_channel_is_always_allowed(monkeypatch):
    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "S")
    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID", "C")
    monkeypatch.delenv("MONOLITH_AGENT_DISCORD_ALLOWED_CHANNEL_IDS", raising=False)

    s = load_settings()

    assert "C" in s.discord_allowed_channel_ids


def test_missing_required_env_raises(monkeypatch):
    monkeypatch.delenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", raising=False)
    monkeypatch.delenv("MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID", raising=False)

    with pytest.raises(KeyError):
        load_settings()
