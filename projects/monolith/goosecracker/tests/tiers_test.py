"""Unit tests for the goosecracker tier -> guest env map (tiers.env_for_tier).

Focus: the ADR 023 6b egress CA is merged into every tier's guest env from the
process env, so goose in the guest trusts the swap sidecar's minted leaf.
"""

from __future__ import annotations

import json

from goosecracker import tiers

_TIERS = {
    "default": {"OPENAI_HOST": "http://model:8080", "GITHUB_TOKEN": "kloak:gh:X"},
    "artifact": {"GOOSE_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "kloak:or:Y"},
}


def _set_tiers(monkeypatch) -> None:
    monkeypatch.setenv("GOOSECRACKER_TIERS", json.dumps(_TIERS))


def test_ca_cert_merged_into_tier_env(monkeypatch):
    _set_tiers(monkeypatch)
    monkeypatch.setenv(
        "EGRESS_CA_CERT", "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----"
    )

    env = tiers.env_for_tier("artifact")
    assert env["GOOSE_PROVIDER"] == "openrouter"
    assert env["OPENROUTER_API_KEY"] == "kloak:or:Y"
    assert "BEGIN CERTIFICATE" in env["EGRESS_CA_CERT"]


def test_ca_cert_absent_when_env_unset(monkeypatch):
    _set_tiers(monkeypatch)
    monkeypatch.delenv("EGRESS_CA_CERT", raising=False)

    env = tiers.env_for_tier("default")
    assert "EGRESS_CA_CERT" not in env


def test_unknown_tier_falls_back_to_default_and_gets_ca(monkeypatch):
    _set_tiers(monkeypatch)
    monkeypatch.setenv("EGRESS_CA_CERT", "PEM")

    env = tiers.env_for_tier("nope")
    assert env["OPENAI_HOST"] == "http://model:8080"
    assert env["EGRESS_CA_CERT"] == "PEM"


def test_explicit_tier_ca_wins_over_process_env(monkeypatch):
    monkeypatch.setenv(
        "GOOSECRACKER_TIERS",
        json.dumps({"default": {"EGRESS_CA_CERT": "tier-pem"}}),
    )
    monkeypatch.setenv("EGRESS_CA_CERT", "process-pem")

    env = tiers.env_for_tier("default")
    assert env["EGRESS_CA_CERT"] == "tier-pem"


# --- Per-tier capability (tool) subset (ADR 034, ADR 039 household) ---------


def test_household_tier_grants_only_household_features():
    granted = tiers.features_for_tier("household")
    assert granted == {"knowledge", "calendar", "reminders"}


def test_household_tier_allows_its_three_families():
    for feature in ("knowledge", "calendar", "reminders"):
        assert tiers.tier_allows("household", feature) is True


def test_household_tier_denies_repo_cluster_artifact():
    for feature in ("repo", "cluster", "artifact"):
        assert tiers.tier_allows("household", feature) is False


def test_default_tier_is_unrestricted():
    # A tier without a restricted entry gets the full capability set, so the
    # household denial is a positive check, not an accidental open set.
    for feature in (
        "knowledge",
        "calendar",
        "reminders",
        "repo",
        "cluster",
        "artifact",
    ):
        assert tiers.tier_allows("default", feature) is True


def test_unknown_and_empty_tier_are_unrestricted():
    assert tiers.tier_allows("nope", "repo") is True
    assert tiers.tier_allows("", "repo") is True
