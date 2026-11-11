"""Bounded improvement previews neither spend quota nor create issues."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from factory.orchestration.factory_improvement_preview import (
    PreviewPolicy,
    fingerprint,
    preview,
    quota_decisions,
)

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


def snapshot():
    return {
        "captured_at": NOW.isoformat(),
        "repo": "jomcgi-org/homelab",
        "intake_complete": True,
        "dedup_complete": True,
        "history_complete": True,
        "rules_reviewed": True,
        "ready_issue_numbers": [],
        "inventory": [],
        "history": [],
        "active_goal_ids": [7],
        "subscription_caps": {
            "account-a": {
                "subscription_only": True,
                "overage_disabled": True,
                "evidence": "reviewed cap configuration",
            }
        },
        "quota": {
            "available": True,
            "grants_complete": True,
            "fetched_at": NOW.isoformat(),
            "grants": {
                "account-a": {
                    "grant": "account-a",
                    "provider": "codex",
                    "observed": True,
                    "observed_at": NOW.isoformat(),
                    "status": "allowed",
                    "exhausted": False,
                    "windows": [
                        {
                            "name": "primary",
                            "used_percent": 0,
                            "window_minutes": 10080,
                            "resets_at": (NOW + timedelta(days=2)).isoformat(),
                            "expired": False,
                        }
                    ],
                }
            },
        },
        "findings": [
            {
                "problem_key": "hikes/sliding-gap",
                "title": "hikes: preserve a second gap",
                "kind": "bug",
                "evidence": "test output at commit abc",
                "reproduction": "pytest exact_test",
                "benefit": "correct route selection",
                "acceptance_tests": "two gap regression passes",
                "owner": "factory refinement",
                "goal_id": 7,
                "ready": True,
                "observed_at": NOW.isoformat(),
            }
        ],
    }


def test_full_remaining_creates_reviewable_draft_without_mutating_input():
    data = snapshot()
    original = deepcopy(data)
    result = preview(data, now=NOW)
    assert result["reason"] == "candidates"
    assert result["quota_decisions"][0]["remaining_percent"] == 100
    assert "Acceptance tests" in result["drafts"][0]["body"]
    assert result["mode"] == "preview_only"
    assert "labels" not in result["drafts"][0]
    assert data == original


@pytest.mark.parametrize("seconds", [0, -1, 302401, 604801])
def test_reset_boundaries_abstain(seconds):
    data = snapshot()
    data["quota"]["grants"]["account-a"]["windows"][0]["resets_at"] = (
        NOW + timedelta(seconds=seconds)
    ).isoformat()
    assert not preview(data, now=NOW)["drafts"]


@pytest.mark.parametrize(
    "used,expected",
    [
        (0, True),
        (50, True),
        (51, False),
        (100, False),
        (101, False),
        (-1, False),
        (float("nan"), False),
        (True, False),
    ],
)
def test_remaining_units(used, expected):
    data = snapshot()
    data["quota"]["grants"]["account-a"]["windows"][0]["used_percent"] = used
    assert bool(preview(data, now=NOW)["drafts"]) is expected


@pytest.mark.parametrize("field", ["captured_at", "fetched_at", "observed_at"])
@pytest.mark.parametrize("age", [901, -1])
def test_stale_and_future_telemetry(field, age):
    data = snapshot()
    target = (
        data
        if field == "captured_at"
        else data["quota"]
        if field == "fetched_at"
        else data["quota"]["grants"]["account-a"]
    )
    target[field] = (NOW - timedelta(seconds=age)).isoformat()
    assert not preview(data, now=NOW)["drafts"]


@pytest.mark.parametrize(
    "change",
    [
        {"reset_source": "estimate"},
        {"resets_at": None},
        {"window_minutes": 0},
        {"expired": True},
    ],
)
def test_unknown_windows_abstain(change):
    data = snapshot()
    data["quota"]["grants"]["account-a"]["windows"][0].update(change)
    assert not preview(data, now=NOW)["drafts"]


def test_binding_short_window_and_conflicting_duration():
    for minutes, used in [(300, 100), (10080, 20)]:
        data = snapshot()
        data["quota"]["grants"]["account-a"]["windows"].append(
            {
                "name": "short",
                "used_percent": used,
                "window_minutes": minutes,
                "resets_at": (NOW + timedelta(hours=1)).isoformat(),
                "expired": False,
            }
        )
        assert not preview(data, now=NOW)["drafts"]


def test_accounts_are_not_combined_or_provider_rollup_used():
    data = snapshot()
    other = deepcopy(data["quota"]["grants"]["account-a"])
    other.update(grant="account-b")
    other["windows"][0]["resets_at"] = (NOW + timedelta(days=6)).isoformat()
    data["quota"]["grants"]["account-b"] = other
    decisions = quota_decisions(data["quota"], now=NOW, policy=PreviewPolicy())
    assert [d["eligible"] for d in decisions] == [True, False]
    del data["quota"]["grants"]["account-a"]
    data["quota"]["providers"] = {"codex": snapshot()["quota"]["grants"]["account-a"]}
    assert not preview(data, now=NOW)["drafts"]


def test_existing_ready_work_wins_even_without_quota():
    data = snapshot()
    data.update(ready_issue_numbers=[7190], quota={})
    assert preview(data, now=NOW)["reason"] == "existing_ready_work"


@pytest.mark.parametrize(
    "key", ["intake_complete", "dedup_complete", "history_complete", "rules_reviewed"]
)
def test_unknown_context_abstains(key):
    data = snapshot()
    del data[key]
    assert preview(data, now=NOW)["reason"] == "context_unknown"


@pytest.mark.parametrize("field", ["subscription_only", "overage_disabled", "evidence"])
def test_cap_attestation_required(field):
    data = snapshot()
    del data["subscription_caps"]["account-a"][field]
    assert preview(data, now=NOW)["reason"] == "no_verified_spare_subscription"


@pytest.mark.parametrize("source", ["issue", "pr", "active_owner"])
def test_dedup_covers_open_work_and_owners(source):
    data = snapshot()
    data["inventory"] = [{"problem_key": "hikes/sliding-gap", "source": source}]
    assert preview(data, now=NOW)["skipped"] == ["duplicate_or_owned"]


def test_duplicate_findings_and_prior_attempts():
    data = snapshot()
    data["findings"] *= 2
    assert len(preview(data, now=NOW)["drafts"]) == 1
    data["history"] = [
        {
            "attempted_at": (NOW - timedelta(days=2)).isoformat(),
            "fingerprint": fingerprint(data["repo"], "hikes/sliding-gap"),
        }
    ]
    assert not preview(data, now=NOW)["drafts"]


@pytest.mark.parametrize(
    "field", ["evidence", "reproduction", "benefit", "acceptance_tests", "owner"]
)
def test_evidence_free_work_is_not_a_ticket(field):
    data = snapshot()
    del data["findings"][0][field]
    assert preview(data, now=NOW)["reason"] == "no_actionable_findings"


def test_empty_findings_and_goal_mismatch():
    data = snapshot()
    data["active_goal_ids"] = []
    assert preview(data, now=NOW)["reason"] == "no_actionable_findings"
    data["findings"] = []
    assert preview(data, now=NOW)["reason"] == "no_actionable_findings"


def test_limits_bound_scouting_and_drafts():
    data = snapshot()
    data["findings"] = [
        {**data["findings"][0], "problem_key": f"problem-{i}"} for i in range(30)
    ]
    result = preview(data, now=NOW)
    assert len(result["drafts"]) == 2
    assert result["findings_truncated"]
    data["findings"][:20] = [{}] * 20
    assert not preview(data, now=NOW)["drafts"]


def test_cooldown_and_useful_completion_feedback():
    data = snapshot()
    data["history"] = [{"attempted_at": NOW.isoformat()}]
    assert preview(data, now=NOW)["reason"] == "cooldown"
    data["history"] = [
        {
            "attempted_at": (NOW - timedelta(days=i)).isoformat(),
            "outcome": "no_findings",
        }
        for i in (2, 3, 4)
    ]
    assert preview(data, now=NOW)["reason"] == "usefulness_review_required"
    data["history"][0].update(
        outcome="useful_completion",
        merged_pr=123,
        acceptance_evidence="regression passed",
    )
    assert preview(data, now=NOW)["drafts"]
    del data["history"][0]["acceptance_evidence"]
    assert preview(data, now=NOW)["reason"] == "usefulness_review_required"


@pytest.mark.parametrize(
    "config",
    [
        {"max_drafts": 3},
        {"max_findings": 100},
        {"max_age_seconds": 901},
        {"cooldown_seconds": 0},
        {"minimum_remaining_percent": float("nan")},
    ],
)
def test_configuration_cannot_remove_bounds(config):
    with pytest.raises(ValueError):
        PreviewPolicy(**config)


@pytest.mark.parametrize("quota", [None, [], "unknown", 1])
def test_non_object_quota_abstains(quota):
    data = snapshot()
    data["quota"] = quota
    result = preview(data, now=NOW)
    assert not result["drafts"]
    assert result["quota_decisions"][0]["reason"] == "quota_inventory_unknown"


@pytest.mark.parametrize("goal", [None, True, "7", 0, -1])
def test_invalid_goal_inventory_abstains(goal):
    data = snapshot()
    data["active_goal_ids"] = [goal]
    data["findings"][0]["goal_id"] = goal
    assert not preview(data, now=NOW)["drafts"]
    del data["findings"][0]["goal_id"]
    assert not preview(data, now=NOW)["drafts"]


def test_missing_finding_goal_and_null_snapshot_abstain():
    data = snapshot()
    del data["findings"][0]["goal_id"]
    assert not preview(data, now=NOW)["drafts"]
    assert preview(None, now=NOW)["reason"] == "context_unknown"


def test_half_week_boundary_is_inclusive():
    data = snapshot()
    data["quota"]["grants"]["account-a"]["windows"][0]["resets_at"] = (
        NOW + timedelta(seconds=302400)
    ).isoformat()
    assert preview(data, now=NOW)["drafts"]


@pytest.mark.parametrize(
    "state", ["conserve", "wait_for_reset", "stale", "unknown", "awaiting_report"]
)
def test_grant_pacing_cannot_be_overridden_by_spare_weekly_quota(state):
    data = snapshot()
    data["quota"]["grants"]["account-a"]["pacing"] = {"state": state}
    result = preview(data, now=NOW)
    assert not result["drafts"]
    assert result["quota_decisions"][0]["reason"] == "broker_pacing_not_permissive"


@pytest.mark.parametrize(
    "used,reset_minutes,eligible", [(95, 5, False), (90, 60, False), (50, 60, True)]
)
def test_short_window_headroom_is_separate_from_weekly_budget(
    used, reset_minutes, eligible
):
    data = snapshot()
    view = data["quota"]["grants"]["account-a"]
    view["pacing"] = {"state": "use_more"}
    view["windows"].append(
        {
            "name": "burst",
            "window_minutes": 300,
            "used_percent": used,
            "resets_at": (NOW + timedelta(minutes=reset_minutes)).isoformat(),
            "expired": False,
        }
    )
    result = preview(data, now=NOW)
    assert bool(result["drafts"]) is eligible
    if not eligible:
        decision = result["quota_decisions"][0]
        assert decision["reason"] == "short_window_conserve"
        assert decision["short_remaining_percent"] == 100 - used


def test_named_grant_conserve_is_not_overridden_by_headline_use_more():
    data = snapshot()
    account = data["quota"]["grants"]["account-a"]
    account["pacing"] = {"state": "conserve"}
    headline = deepcopy(account)
    headline["pacing"] = {"state": "use_more"}
    data["quota"]["providers"] = {"codex": headline}
    assert not preview(data, now=NOW)["drafts"]
