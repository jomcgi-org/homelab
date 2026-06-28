# projects/monolith/agent/config_test.py
import pytest

from agent.config import load_settings


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
