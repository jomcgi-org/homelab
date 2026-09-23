import factory.orchestration.config as config


def test_defaults(monkeypatch):
    for name in (
        "SWARM_ENABLED",
        "SWARM_IMPLEMENTER_MODEL",
        "SWARM_REVIEWER_MODEL",
        "SWARM_MAX_ATTEMPTS",
        "SWARM_MAX_REVIEW_CYCLES",
        "SWARM_TURN_TIMEOUT_SECONDS",
        "SWARM_DECISION_TIMEOUT_SECONDS",
        "SWARM_CODEX_CONCURRENCY",
        "FACTORY_LOST_BEFORE_SESSION_SWEEP_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    assert config.enabled() is False
    assert config.implementer_model() == "luna"
    assert config.reviewer_model() == "opus"
    assert config.max_attempts() == 2
    assert config.max_review_cycles() == 2
    assert config.turn_timeout_seconds() == 1800
    assert config.decision_timeout_seconds() == 86400
    assert config.codex_concurrency() == 2
    assert config.factory_lost_before_session_sweep_enabled() is False


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")
    monkeypatch.setenv("SWARM_IMPLEMENTER_MODEL", "cheap")
    monkeypatch.setenv("SWARM_REVIEWER_MODEL", "careful")
    monkeypatch.setenv("SWARM_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("SWARM_MAX_REVIEW_CYCLES", "3")
    monkeypatch.setenv("SWARM_TURN_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("SWARM_DECISION_TIMEOUT_SECONDS", "34")
    monkeypatch.setenv("SWARM_CODEX_CONCURRENCY", "7")
    monkeypatch.setenv("FACTORY_LOST_BEFORE_SESSION_SWEEP_ENABLED", "true")
    assert config.enabled() is True
    assert config.implementer_model() == "cheap"
    assert config.reviewer_model() == "careful"
    assert config.max_attempts() == 4
    assert config.max_review_cycles() == 3
    assert config.turn_timeout_seconds() == 12
    assert config.decision_timeout_seconds() == 34
    assert config.codex_concurrency() == 7
    assert config.factory_lost_before_session_sweep_enabled() is True
