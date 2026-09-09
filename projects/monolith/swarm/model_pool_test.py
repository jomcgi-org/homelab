import pytest

from swarm import model_pool


def policy(**overrides):
    base = {
        "allowed_models": ["astra", "spark", "sol", "sonnet", "opus"],
        "conductor_model": "astra",
        "worker_model": "sol",
        "reviewer_model": "opus",
        "model_pools": {"conductor": ["astra", "spark"], "worker": ["sol", "sonnet"]},
    }
    return {**base, **overrides}


def test_preferred_model_wins_when_its_provider_has_quota():
    choice = model_pool.select_model(
        "conductor", policy(), quota={"codex": {"headline_used_percent": 40.0}}
    )
    assert choice["model"] == "astra"
    assert choice["fallback_from"] is None
    assert choice["skipped"] == []


def test_exhausted_provider_moves_to_next_pool_member():
    choice = model_pool.select_model(
        "conductor", policy(), quota={"codex": {"exhausted": True}}
    )
    assert choice["model"] == "spark"
    assert choice["fallback_from"] == "astra"
    assert choice["skipped"] == [{"model": "astra", "reason": "exhausted"}]
    assert choice["reason"] == "no_quota_feed"


def test_fresh_headline_at_threshold_counts_as_exhausted(monkeypatch):
    monkeypatch.setenv("SWARM_MODEL_POOL_EXHAUSTED_PERCENT", "95")
    quota = {"codex": {"headline_used_percent": 95.0, "age_seconds": 30.0}}
    choice = model_pool.select_model("worker", policy(), quota=quota)
    assert choice["model"] == "sonnet"
    assert choice["skipped"][0]["reason"] == "used_percent 95"


def test_stale_headline_does_not_route_away_from_preferred():
    quota = {"codex": {"headline_used_percent": 99.0, "age_seconds": 4000.0}}
    choice = model_pool.select_model("worker", policy(), quota=quota)
    assert choice["model"] == "sol"
    assert choice["reason"].startswith("stale_observation")


def test_unobserved_provider_keeps_preferred():
    choice = model_pool.select_model("worker", policy(), quota={})
    assert choice["model"] == "sol"
    assert choice["reason"] == "unobserved"


def test_pool_with_every_provider_walled_keeps_preferred_and_records_evidence():
    quota = {"codex": {"exhausted": True}, "claude": {"exhausted": True}}
    choice = model_pool.select_model("worker", policy(), quota=quota)
    assert choice["model"] == "sol"
    assert choice["fallback_from"] is None
    assert choice["reason"] == "pool_exhausted"
    assert [s["model"] for s in choice["skipped"]] == ["sol", "sonnet"]


def test_missing_pools_use_the_single_role_model():
    single = policy()
    del single["model_pools"]
    choice = model_pool.select_model(
        "conductor", single, quota={"codex": {"exhausted": True}}
    )
    assert choice["model"] == "astra"
    assert choice["reason"] == "pool_exhausted"


def test_review_is_not_a_pooled_role():
    with pytest.raises(ValueError):
        model_pool.select_model("review", policy(), quota={})


def test_unknown_model_has_no_quota_feed():
    assert model_pool.availability("not-a-model", {"codex": {"exhausted": True}}) == (
        True,
        "no_quota_feed",
    )


def test_selection_reason_carries_fallback_evidence_within_limit():
    choice = model_pool.select_model(
        "conductor", policy(), quota={"codex": {"exhausted": True}}
    )
    reason = model_pool.selection_reason("Reconcile task evidence", choice)
    assert reason == (
        "Reconcile task evidence (model fallback astra -> spark: astra exhausted)"
    )
    assert model_pool.selection_reason("x", {"fallback_from": None}) == "x"
    assert len(model_pool.selection_reason("y" * 300, choice)) == 256


def test_broker_failure_reads_as_unobserved(monkeypatch):
    import agent_sessions.provider_quota as quota

    def boom(**_kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(quota, "fetch_provider_quota_sync", boom)
    assert model_pool.quota_summary() == {}
    assert model_pool.select_model("worker", policy())["model"] == "sol"
