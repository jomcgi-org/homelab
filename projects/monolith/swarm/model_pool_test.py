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
    assert choice["reason"] == "single_member_pool"


def test_review_is_not_a_pooled_role():
    with pytest.raises(ValueError):
        model_pool.select_model("review", policy(), quota={})


def test_unsupported_model_is_never_selected():
    assert model_pool.availability("not-a-model", {}) == (False, "unsupported_model")
    typo = policy(
        allowed_models=["sol", "sonnett"],
        model_pools={"conductor": ["astra", "spark"], "worker": ["sol", "sonnett"]},
    )
    choice = model_pool.select_model(
        "worker", typo, quota={"codex": {"exhausted": True}}
    )
    assert choice["model"] == "sol"
    assert choice["reason"] == "pool_exhausted"
    assert choice["skipped"][1] == {"model": "sonnett", "reason": "unsupported_model"}


def test_exhausted_holds_until_a_known_reset_regardless_of_age():
    quota = {
        "codex": {
            "exhausted": True,
            "age_seconds": 36000.0,
            "resets_at": "2999-01-01T00:00:00Z",
        }
    }
    assert (
        model_pool.select_model("conductor", policy(), quota=quota)["model"] == "spark"
    )


def test_exhausted_expires_once_the_known_reset_has_passed():
    quota = {
        "codex": {
            "exhausted": True,
            "age_seconds": 5.0,
            "resets_at": "2000-01-01T00:00:00+00:00",
        }
    }
    choice = model_pool.select_model("conductor", policy(), quota=quota)
    assert choice["model"] == "astra"
    assert choice["reason"] == "reset_passed"


def test_exhausted_without_reset_time_expires_with_the_observation():
    stale = {"codex": {"exhausted": True, "age_seconds": 901.0}}
    assert (
        model_pool.select_model("conductor", policy(), quota=stale)["model"] == "astra"
    )
    fresh = {"codex": {"exhausted": True, "age_seconds": 899.0}}
    assert (
        model_pool.select_model("conductor", policy(), quota=fresh)["model"] == "spark"
    )


def test_reset_passed_accepts_epoch_and_rejects_garbage():
    assert model_pool.reset_passed(0) is True
    assert model_pool.reset_passed("not a time") is None
    assert model_pool.reset_passed(None) is None
    assert model_pool.reset_passed(True) is None


def test_single_member_pool_never_reads_the_broker(monkeypatch):
    monkeypatch.setattr(
        model_pool, "quota_summary", lambda: pytest.fail("broker must not be read")
    )
    single = policy()
    del single["model_pools"]
    choice = model_pool.select_model("worker", single)
    assert choice == {
        "model": "sol",
        "preferred": "sol",
        "fallback_from": None,
        "skipped": [],
        "reason": "single_member_pool",
    }


def test_malformed_tunables_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("SWARM_MODEL_POOL_EXHAUSTED_PERCENT", "97%")
    monkeypatch.setenv("SWARM_MODEL_POOL_QUOTA_MAX_AGE_SECONDS", "soon")
    assert model_pool.exhausted_percent() == 97.0
    assert model_pool.max_quota_age_seconds() == 900.0


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
