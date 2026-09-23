from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from factory import baseline

START = datetime(2026, 9, 16, tzinfo=timezone.utc)
END = datetime(2026, 9, 23, tzinfo=timezone.utc)
GENERATED = datetime(2026, 9, 23, 1, 50, tzinfo=timezone.utc)


def event(event_id: str, event_type: str, occurred_at: str, **values) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "revision": 1,
        "evidence_url": f"https://evidence.example/{event_id}",
        **values,
    }


def source(kind: str, **values) -> dict:
    return {
        "kind": kind,
        "complete": True,
        "coverage_start": "2026-09-16T00:00:00Z",
        "coverage_end": "2026-09-23T00:00:00Z",
        "query": f"test {kind} query",
        **values,
    }


def document(events: list[dict], *, sources: list[dict] | None = None, apps=None):
    return {
        "contract_version": 1,
        "repository": "example/repo",
        "source_as_of": "2026-09-23T01:45:00Z",
        "sources": sources or [],
        "managed_applications": apps or [],
        "events": events,
    }


def report(events: list[dict], *, sources: list[dict] | None = None, apps=None):
    return baseline.build_report(
        document(events, sources=sources, apps=apps),
        generated_at=GENERATED,
        start=START,
        end=END,
    )


def test_traceable_success_failure_intervention_and_all_cohort_costs():
    events = [
        event("i1", "task_intake", "2026-09-16T01:00:00Z", task_id="t1"),
        event(
            "a1",
            "agent_attempt_finished",
            "2026-09-16T02:00:00Z",
            task_id="t1",
            outcome="failed",
            cost_usd=2.0,
            reserved_cost_usd=5.0,
        ),
        event(
            "r1",
            "review_correction",
            "2026-09-16T03:00:00Z",
            task_id="t1",
            outcome="failed_review",
            cost_usd=3.0,
        ),
        event(
            "op1",
            "operator_intervention",
            "2026-09-16T03:30:00Z",
            task_id="t1",
            actor="operator@example.test",
        ),
        event("v1", "verified_outcome", "2026-09-16T05:00:00Z", task_id="t1"),
        event("i2", "task_intake", "2026-09-17T01:00:00Z", task_id="t2"),
        event(
            "a2",
            "agent_attempt_finished",
            "2026-09-17T02:00:00Z",
            task_id="t2",
            outcome="failed",
            cost_usd=7.0,
        ),
        event("x2", "task_abandoned", "2026-09-17T03:00:00Z", task_id="t2"),
        event(
            "signal",
            "intervention_required",
            "2026-09-17T02:30:00Z",
            task_id="t2",
        ),
    ]
    result = report(
        events,
        sources=[
            source("factory"),
            source("audit"),
            source("cost"),
            source("github"),
        ],
    )

    assert result["counts"] == {
        "cohort_tasks": 2,
        "accepted_changes": 1,
        "incomplete": 0,
        "cancelled": 0,
        "abandoned": 1,
        "failed_agent_attempts": 2,
        "intervention_required_signals": 1,
        "explicit_operator_interventions": 1,
        "repository_closed_unmerged_attempts": 0,
    }
    assert (
        result["metrics"]["factory"]["intake_to_verified_outcome"]["median"] == 4 * 3600
    )
    assert result["metrics"]["factory"]["rework_rate"]["value"] == 1
    assert (
        result["metrics"]["factory"]["interventions_per_accepted_change"]["value"] == 1
    )
    cost = result["metrics"]["factory"]["cohort_cost_per_accepted_change"]
    assert cost["value"] == 12.0
    assert cost["reserved_ceiling_subtotal_usd"] == 5.0
    assert cost["known_cost_items"] == cost["total_expected_cost_items"] == 3
    assert "https://evidence.example/a2" in cost["evidence"]


def test_duplicate_reordered_replayed_and_revised_inputs_are_deterministic():
    original = event("i", "task_intake", "2026-09-16T01:00:00Z", task_id="t1")
    old_attempt = event(
        "a",
        "agent_attempt_finished",
        "2026-09-16T02:00:00Z",
        task_id="t1",
        outcome="failed",
        cost_usd=9.0,
    )
    corrected = {**old_attempt, "revision": 2, "cost_usd": 2.0}
    verified = event("v", "verified_outcome", "2026-09-16T03:00:00Z", task_id="t1")
    first = report(
        [verified, old_attempt, original, original.copy(), corrected],
        sources=[source("factory"), source("cost")],
    )
    second = report(
        [corrected, original, verified, old_attempt, original.copy()],
        sources=[source("factory"), source("cost")],
    )

    assert first["metrics"] == second["metrics"]
    assert (
        first["metrics"]["factory"]["cohort_cost_per_accepted_change"]["value"] == 2.0
    )
    assert first["data_quality"]["exact_replays_dropped"] == 1
    assert (
        first["data_quality"]["revised_events"]
        + first["data_quality"]["older_revisions_dropped"]
        == 1
    )


def test_conflicting_same_revision_is_rejected():
    one = event("i", "task_intake", "2026-09-16T01:00:00Z", task_id="t1")
    two = {**one, "task_id": "t2"}
    with pytest.raises(ValueError, match="conflicting replay"):
        report([one, two])


def test_half_open_boundaries_and_right_censoring():
    result = report(
        [
            event("before", "task_intake", "2026-09-15T23:59:59Z", task_id="before"),
            event("start", "task_intake", "2026-09-16T00:00:00Z", task_id="start"),
            event("end", "verified_outcome", "2026-09-23T00:00:00Z", task_id="start"),
            event("after", "task_intake", "2026-09-23T00:00:00Z", task_id="after"),
        ],
        sources=[source("factory")],
    )
    assert result["counts"]["cohort_tasks"] == 1
    assert result["counts"]["accepted_changes"] == 0
    assert result["counts"]["incomplete"] == 1
    assert result["data_quality"]["right_censored_tasks"] == 1
    assert (
        result["metrics"]["factory"]["intake_to_verified_outcome"]["status"]
        == "unavailable"
    )


def test_missing_partial_cost_and_zero_accepted_denominator_stay_unavailable():
    result = report(
        [
            event("i", "task_intake", "2026-09-16T01:00:00Z", task_id="t1"),
            event(
                "a",
                "agent_attempt_finished",
                "2026-09-16T02:00:00Z",
                task_id="t1",
                outcome="failed",
                cost_usd=1.0,
            ),
            event(
                "r",
                "review_correction",
                "2026-09-16T03:00:00Z",
                task_id="t1",
                cost_usd=None,
                reserved_cost_usd=4.0,
            ),
            event("c", "task_cancelled", "2026-09-16T04:00:00Z", task_id="t1"),
        ],
        sources=[source("factory"), source("cost")],
    )
    metric = result["metrics"]["factory"]["cohort_cost_per_accepted_change"]
    assert metric["status"] == "unavailable"
    assert metric["value"] is None
    assert metric["known_cost_subtotal_usd"] == 1.0
    assert metric["complete_cohort_cost_usd"] is None
    assert metric["known_cost_items"] == 1
    assert metric["total_expected_cost_items"] == 2
    assert metric["reserved_ceiling_subtotal_usd"] == 4.0


def test_corrected_reopened_evidence_removes_prior_acceptance():
    verified = event(
        "verified", "verified_outcome", "2026-09-16T03:00:00Z", task_id="t1"
    )
    reopened = event(
        "reopened",
        "outcome_reopened",
        "2026-09-16T04:00:00Z",
        task_id="t1",
        supersedes_event_id="verified",
    )
    result = report(
        [
            event("intake", "task_intake", "2026-09-16T01:00:00Z", task_id="t1"),
            verified,
            reopened,
        ],
        sources=[source("factory")],
    )
    assert result["counts"]["accepted_changes"] == 0
    assert result["counts"]["incomplete"] == 1
    assert result["data_quality"]["superseded_events"] == 1


def test_overlapping_attempts_are_not_summed_as_elapsed_time():
    result = report(
        [
            event("i", "task_intake", "2026-09-16T00:00:00Z", task_id="t1"),
            event(
                "a1",
                "agent_attempt_finished",
                "2026-09-16T03:00:00Z",
                task_id="t1",
                started_at="2026-09-16T01:00:00Z",
                outcome="failed",
                cost_usd=1,
            ),
            event(
                "a2",
                "agent_attempt_finished",
                "2026-09-16T03:30:00Z",
                task_id="t1",
                started_at="2026-09-16T01:30:00Z",
                outcome="succeeded",
                cost_usd=1,
            ),
            event("v", "verified_outcome", "2026-09-16T04:00:00Z", task_id="t1"),
        ],
        sources=[source("factory"), source("cost")],
    )
    timing = result["metrics"]["factory"]["intake_to_verified_outcome"]
    assert timing["median"] == 4 * 3600
    assert result["counts"]["failed_agent_attempts"] == 1


def test_merge_deploy_and_verify_remain_separate_with_production_recovery():
    app = {"name": "api", "environment": "production", "service_boundary": "api"}
    result = report(
        [
            event("i", "task_intake", "2026-09-16T00:00:00Z", task_id="t1"),
            event(
                "o",
                "pr_opened",
                "2026-09-16T00:30:00Z",
                change_id="pr:1",
            ),
            event(
                "m",
                "pr_merged",
                "2026-09-16T01:00:00Z",
                task_id="t1",
                change_id="pr:1",
            ),
            event(
                "m-join-fanout",
                "pr_merged",
                "2026-09-16T01:00:00Z",
                task_id="t1",
                change_id="pr:1",
            ),
            event(
                "d",
                "deployment_failed",
                "2026-09-16T02:00:00Z",
                change_id="pr:1",
                deployment_id="deploy:1",
                application="api",
                environment="production",
            ),
            event(
                "d-join-fanout",
                "deployment_failed",
                "2026-09-16T02:00:00Z",
                change_id="pr:1",
                deployment_id="deploy:1",
                application="api",
                environment="production",
            ),
            event(
                "recovery",
                "deployment_recovered",
                "2026-09-16T03:00:00Z",
                change_id="pr:1",
                deployment_id="deploy:1",
                application="api",
                environment="production",
            ),
        ],
        sources=[
            source("factory"),
            source("github"),
            source("deployment", application="api", environment="production"),
            source("incident", application="api", environment="production"),
        ],
        apps=[app],
    )
    assert result["counts"]["accepted_changes"] == 0
    assert result["metrics"]["repository_delivery"]["merge_count"]["value"] == 1
    production = result["metrics"]["managed_production_applications"][0]
    assert production["production_lead_time"]["median"] == 3600
    assert production["deployment_frequency"]["numerator"] == 1
    assert production["change_failure_rate"]["value"] == 1
    assert production["failed_deployment_recovery_time"]["median"] == 3600
    assert production["production_rework_rate"]["value"] == 1


def test_missing_deployment_and_incident_coverage_never_becomes_zero_success():
    app = {"name": "api", "environment": "production", "service_boundary": "api"}
    partial = {
        **source("deployment", application="api", environment="production"),
        "coverage_start": "2026-09-17T00:00:00Z",
    }
    result = report(
        [
            event(
                "d",
                "deployment_succeeded",
                "2026-09-17T01:00:00Z",
                change_id="pr:1",
                deployment_id="deploy:1",
                application="api",
                environment="production",
            )
        ],
        sources=[partial],
        apps=[app],
    )
    production = result["metrics"]["managed_production_applications"][0]
    for name in (
        "deployment_frequency",
        "production_lead_time",
        "change_failure_rate",
        "failed_deployment_recovery_time",
        "production_rework_rate",
    ):
        assert production[name]["status"] == "unavailable"
        assert production[name].get("value") is None


def test_csv_export_preserves_unavailable_state_and_reason():
    result = report([])
    output = io.StringIO()
    baseline.write_csv(result, output)
    text = output.getvalue()
    assert "factory,intake_to_verified_outcome,unavailable" in text
    assert "factory history does not span the window" in text


def test_default_window_is_seven_completed_utc_days():
    start, end = baseline.completed_window(
        datetime(2026, 9, 23, 23, 59, 59, tzinfo=timezone.utc)
    )
    assert start == START
    assert end == END
