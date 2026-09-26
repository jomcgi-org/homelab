"""Tests for the pure rollout verdict shared by both MCP surfaces."""

from __future__ import annotations

import copy

import pytest

from shared.rollout import FAILED, IN_PROGRESS, VERIFIED, RevisionMismatch, verdict

GIT = "a" * 40

APP = {
    "metadata": {"name": "monolith"},
    "spec": {
        "sources": [
            {
                "repoURL": "ghcr.io/jomcgi/homelab/charts",
                "chart": "monolith",
                "targetRevision": "0.546.4",
            },
            {
                "repoURL": "https://github.com/jomcgi-org/homelab.git",
                "targetRevision": "HEAD",
                "ref": "values",
            },
        ]
    },
    "status": {
        "sync": {"status": "Synced", "revisions": ["0.546.4", GIT]},
        "health": {"status": "Healthy"},
        "operationState": {
            "phase": "Succeeded",
            "syncResult": {"revisions": ["0.546.4", GIT]},
        },
        "reconciledAt": "2026-09-26T22:00:00Z",
        "summary": {"images": ["ghcr.io/jomcgi/homelab/projects/monolith/image:x"]},
        "resources": [
            {"kind": "Deployment", "name": "monolith", "health": {"status": "Healthy"}},
            {"kind": "ConfigMap", "name": "cfg"},
        ],
    },
}


def _app(**status_overrides):
    app = copy.deepcopy(APP)
    app["status"].update(status_overrides)
    return app


def _check(result, name):
    return next(c for c in result["checks"] if c["name"] == name)


# --- happy path and expected revision -----------------------------------


def test_synced_healthy_app_with_target_applied_is_verified():
    result = verdict(_app())
    assert result["verdict"] == VERIFIED
    assert result["target_revision"] == result["live_revision"] == "0.546.4"
    assert result["unhealthy_resource_count"] == 0
    assert result["images"]


def test_expected_revision_reached_or_passed_is_verified():
    assert verdict(_app(), expected_revision="0.546.4")["verdict"] == VERIFIED
    assert verdict(_app(), expected_revision="0.99.0")["verdict"] == VERIFIED
    assert verdict(_app(), expected_revision="0.100.0")["verdict"] == VERIFIED


def test_expected_revision_not_reached_is_in_progress():
    result = verdict(_app(), expected_revision="0.546.5")
    assert result["verdict"] == IN_PROGRESS
    assert _check(result, "expected_revision")["state"] == IN_PROGRESS


# --- target vs applied ---------------------------------------------------


def test_refreshed_target_not_yet_applied_is_in_progress():
    """After refresh, sync.revisions is the new target before anything is applied."""
    app = _app(
        sync={"status": "OutOfSync", "revisions": ["0.546.5", GIT]},
        history=[{"revisions": ["0.546.4", GIT]}],
    )
    app["spec"]["sources"][0]["targetRevision"] = "0.546.5"
    result = verdict(app, expected_revision="0.546.5")
    assert result["verdict"] == IN_PROGRESS
    assert result["live_revision"] == "0.546.4"
    assert _check(result, "sync")["state"] == IN_PROGRESS
    assert _check(result, "expected_revision")["state"] == IN_PROGRESS


def test_last_applied_falls_back_to_history_when_no_successful_operation():
    app = _app(operationState=None, history=[{"revisions": ["0.546.4", GIT]}])
    assert verdict(app)["last_applied_revision"] == "0.546.4"
    assert verdict(app)["verdict"] == VERIFIED


def test_synced_git_app_on_newer_main_without_a_sync_is_verified():
    """A main commit that renders identically leaves the app Synced, no operation."""
    app = _git_app("b" * 40)
    app["status"]["operationState"]["syncResult"]["revision"] = "a" * 40
    app["status"]["history"] = [{"revision": "a" * 40}]
    result = verdict(app)
    assert result["verdict"] == VERIFIED
    assert result["live_revision"] == "b" * 40
    assert result["last_applied_revision"] == "a" * 40
    assert verdict(app, expected_revision="bbbbbbb")["verdict"] == VERIFIED


def test_requested_pin_not_yet_refreshed_is_in_progress():
    """A lowered pin (the revert lever) is not verified before ArgoCD targets it."""
    app = _app()
    app["spec"]["sources"][0]["targetRevision"] = "0.546.3"
    result = verdict(app)
    assert result["verdict"] == IN_PROGRESS
    assert _check(result, "refresh")["state"] == IN_PROGRESS


# --- health ----------------------------------------------------------------


def test_progressing_health_is_in_progress_and_lists_the_resource():
    app = _app(
        health={"status": "Progressing"},
        resources=[
            {
                "kind": "Deployment",
                "namespace": "monolith",
                "name": "monolith",
                "health": {"status": "Progressing", "message": "1 of 2 updated"},
            }
        ],
    )
    result = verdict(app)
    assert result["verdict"] == IN_PROGRESS
    assert result["unhealthy_resources"] == [
        {
            "kind": "Deployment",
            "namespace": "monolith",
            "name": "monolith",
            "health": "Progressing",
            "message": "1 of 2 updated",
        }
    ]


def test_missing_while_syncing_is_in_progress():
    app = _app(
        health={"status": "Missing"},
        sync={"status": "OutOfSync", "revisions": ["0.546.4", GIT]},
    )
    assert verdict(app)["verdict"] == IN_PROGRESS
    running = _app(health={"status": "Missing"}, operationState={"phase": "Running"})
    assert verdict(running)["verdict"] == IN_PROGRESS


def test_missing_after_sync_is_failed():
    assert verdict(_app(health={"status": "Missing"}))["verdict"] == FAILED


def test_suspended_is_verified_and_not_listed_unhealthy():
    app = _app(
        health={"status": "Suspended"},
        resources=[
            {"kind": "CronWorkflow", "name": "grid", "health": {"status": "Suspended"}}
        ],
    )
    result = verdict(app)
    assert result["verdict"] == VERIFIED
    assert result["unhealthy_resource_count"] == 0


def test_degraded_is_failed():
    assert verdict(_app(health={"status": "Degraded"}))["verdict"] == FAILED


def test_unknown_health_is_never_verified():
    assert verdict(_app(health={"status": "Unknown"}))["verdict"] == IN_PROGRESS


# --- operations and conditions ------------------------------------------


def test_failed_operation_for_current_target_is_failed():
    app = _app(
        operationState={
            "phase": "Failed",
            "message": "hook failed",
            "syncResult": {"revisions": ["0.546.4", GIT]},
        }
    )
    result = verdict(app)
    assert result["verdict"] == FAILED
    assert "hook failed" in _check(result, "operation")["detail"]


def test_stale_failed_operation_after_revert_does_not_pin_failure():
    app = _app(
        operationState={
            "phase": "Failed",
            "message": "admission webhook denied",
            "operation": {"sync": {"revisions": ["0.546.5", GIT]}},
        },
        history=[{"revisions": ["0.546.4", GIT]}],
    )
    result = verdict(app)
    assert result["verdict"] == VERIFIED
    assert _check(result, "operation")["state"] == VERIFIED


def test_running_operation_is_in_progress():
    assert verdict(_app(operationState={"phase": "Running"}))["verdict"] == IN_PROGRESS


def test_error_condition_is_failed_with_message():
    app = _app(
        sync={"status": "Unknown", "revisions": ["0.546.4", GIT]},
        conditions=[
            {"type": "SharedResourceWarning", "message": "fine"},
            {
                "type": "ComparisonError",
                "message": "failed to render chart: bad values",
            },
        ],
    )
    result = verdict(app)
    assert result["verdict"] == FAILED
    assert "bad values" in _check(result, "conditions")["detail"]


def test_warning_conditions_alone_do_not_fail():
    app = _app(conditions=[{"type": "OrphanedResourceWarning", "message": "x"}])
    assert verdict(app)["verdict"] == VERIFIED


# --- git-tracked apps ----------------------------------------------------


def _git_app(revision):
    return {
        "metadata": {"name": "otel-collector"},
        "spec": {
            "source": {"repoURL": "https://x", "path": "p", "targetRevision": "HEAD"}
        },
        "status": {
            "sync": {"status": "Synced", "revision": revision},
            "health": {"status": "Healthy"},
            "operationState": {
                "phase": "Succeeded",
                "syncResult": {"revision": revision},
            },
        },
    }


def test_git_app_matches_commit_prefix():
    app = _git_app("abcdef1234" + "0" * 30)
    assert verdict(app)["verdict"] == VERIFIED
    assert verdict(app, expected_revision="abcdef1")["verdict"] == VERIFIED
    assert verdict(app, expected_revision="1234567")["verdict"] == IN_PROGRESS


def test_chart_version_against_git_app_is_rejected_not_stalled():
    with pytest.raises(RevisionMismatch):
        verdict(_git_app("abcdef1234" + "0" * 30), expected_revision="0.12.3")


def test_commit_against_chart_app_is_rejected():
    with pytest.raises(RevisionMismatch):
        verdict(_app(), expected_revision="abcdef1")


# --- robustness ------------------------------------------------------------


def test_empty_object_is_never_verified():
    result = verdict({})
    assert result["verdict"] == IN_PROGRESS
    assert result["app"] is None


def test_unhealthy_resources_are_bounded():
    resources = [
        {"kind": "Pod", "name": f"p{i}", "health": {"status": "Degraded"}}
        for i in range(40)
    ]
    result = verdict(_app(health={"status": "Degraded"}, resources=resources))
    assert result["unhealthy_resource_count"] == 40
    assert len(result["unhealthy_resources"]) == 10
