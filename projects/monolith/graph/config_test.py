import graph.config as config


def test_defaults(monkeypatch):
    for name in (
        "GRAPH_ENABLED",
        "GRAPH_IMPLEMENTER_MODEL",
        "GRAPH_REVIEWER_MODEL",
        "GRAPH_MAX_ATTEMPTS",
        "GRAPH_TURN_TIMEOUT_SECONDS",
        "GRAPH_CODEX_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)
    assert config.enabled() is False
    assert config.implementer_model() == "luna"
    assert config.reviewer_model() == "opus"
    assert config.max_attempts() == 2
    assert config.turn_timeout_seconds() == 1800
    assert config.codex_concurrency() == 2


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("GRAPH_ENABLED", "true")
    monkeypatch.setenv("GRAPH_IMPLEMENTER_MODEL", "cheap")
    monkeypatch.setenv("GRAPH_REVIEWER_MODEL", "careful")
    monkeypatch.setenv("GRAPH_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("GRAPH_TURN_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("GRAPH_CODEX_CONCURRENCY", "7")
    assert config.enabled() is True
    assert config.implementer_model() == "cheap"
    assert config.reviewer_model() == "careful"
    assert config.max_attempts() == 4
    assert config.turn_timeout_seconds() == 12
    assert config.codex_concurrency() == 7
