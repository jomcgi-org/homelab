"""Tests for the pure rollout verdict shared by both MCP surfaces."""

from __future__ import annotations

import copy

import pytest

from shared.rollout import (
    FAILED,
    IN_PROGRESS,
    VERIFIED,
    RevisionMismatch,
    kargo_stage_ref,
    verdict,
)

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


# --- Kargo-owned apps -------------------------------------------------------

CHART_REPO = "oci://ghcr.io/jomcgi/homelab/charts/monolith"


def _kargo_app(live="0.546.4", **status_overrides):
    app = _app(**status_overrides)
    app["metadata"]["annotations"] = {
        "kargo.akuity.io/authorized-stage": "kargo-monolith:prod"
    }
    app["spec"]["sources"][0]["targetRevision"] = live
    app["status"]["sync"]["revisions"] = [live, GIT]
    app["status"]["operationState"]["syncResult"]["revisions"] = [live, GIT]
    return app


def _freight_ref(version):
    return {
        "name": f"f-{version}",
        "charts": [{"repoURL": CHART_REPO, "version": version}],
    }


def _promotion(version, phase, **status):
    return {
        "name": f"prod.{version}",
        "freight": _freight_ref(version),
        "status": {"phase": phase, **status},
    }


def _stage(last=None, current=None, upstream=(), auto=True):
    status = {"autoPromotionEnabled": auto}
    if last:
        status["lastPromotion"] = last
    if current:
        status["currentPromotion"] = current
    return {
        "metadata": {"name": "prod"},
        "spec": {
            "requestedFreight": [{"sources": {"stages": list(upstream)}}]
            if upstream
            else [{"sources": {"direct": True}}]
        },
        "status": status,
    }


def _freight(version, alias=None, verified=(), approved=(), current=()):
    return {
        "metadata": {"name": f"f-{version}"},
        "alias": alias or f"alias-{version}",
        "charts": [{"repoURL": CHART_REPO, "version": version}],
        "status": {
            "verifiedIn": {s: {} for s in verified},
            "approvedFor": {s: {} for s in approved},
            "currentlyIn": {s: {} for s in current},
        },
    }


def test_kargo_stage_ref_reads_the_authorized_stage_annotation():
    assert kargo_stage_ref(_kargo_app()) == ("kargo-monolith", "prod")
    assert kargo_stage_ref(_app()) is None
    bad = _kargo_app()
    bad["metadata"]["annotations"]["kargo.akuity.io/authorized-stage"] = "no-colon"
    assert kargo_stage_ref(bad) is None


def test_app_without_the_annotation_ignores_kargo_context():
    result = verdict(_app(), kargo={"stage": _stage(), "freights": []})
    assert "kargo" not in result
    assert result["verdict"] == VERIFIED


def test_kargo_block_reports_promotions_without_changing_a_verified_verdict():
    stage = _stage(last=_promotion("0.546.4", "Succeeded", finishedAt="t"))
    result = verdict(_kargo_app(), kargo={"stage": stage, "freights": []})
    assert result["verdict"] == VERIFIED
    assert result["kargo"]["stage"] == "prod"
    assert result["kargo"]["last_promotion"]["version"] == "0.546.4"
    assert result["kargo"]["last_promotion"]["phase"] == "Succeeded"


def test_failed_promotion_for_the_expected_version_fails_with_its_message():
    stage = _stage(
        last=_promotion("0.547.0", "Errored", message="argocd-wait timed out")
    )
    result = verdict(
        _kargo_app(),
        expected_revision="0.547.0",
        kargo={"stage": stage, "freights": [_freight("0.547.0")]},
    )
    assert result["verdict"] == FAILED
    detail = _check(result, "kargo")["detail"]
    assert "Errored" in detail and "argocd-wait timed out" in detail
    assert "does not retry" in detail


def test_failed_promotion_for_an_older_version_does_not_fail_a_newer_expectation():
    stage = _stage(last=_promotion("0.546.9", "Failed", message="old"))
    result = verdict(
        _kargo_app(),
        expected_revision="0.547.0",
        kargo={"stage": stage, "freights": [_freight("0.547.0")]},
    )
    assert result["verdict"] == IN_PROGRESS
    assert _check(result, "kargo")["state"] == IN_PROGRESS


def test_running_promotion_is_in_progress_with_its_step():
    stage = _stage(current=_promotion("0.547.0", "Running", currentStep=2))
    result = verdict(
        _kargo_app(),
        expected_revision="0.547.0",
        kargo={"stage": stage, "freights": [_freight("0.547.0")]},
    )
    assert result["verdict"] == IN_PROGRESS
    assert "at step 2" in _check(result, "kargo")["detail"]


def test_undiscovered_chart_is_in_progress():
    result = verdict(
        _kargo_app(),
        expected_revision="0.547.0",
        kargo={"stage": _stage(), "freights": [_freight("0.546.4")]},
    )
    assert "has not discovered chart 0.547.0" in _check(result, "kargo")["detail"]


def test_freight_waiting_on_upstream_verification_names_the_stage():
    result = verdict(
        _kargo_app(),
        expected_revision="0.547.0",
        kargo={
            "stage": _stage(upstream=["dev"]),
            "freights": [_freight("0.547.0", alias="brave-otter")],
        },
    )
    detail = _check(result, "kargo")["detail"]
    assert "brave-otter waits for verification in dev" in detail


def test_manual_approval_is_visible_on_the_expected_freight():
    result = verdict(
        _kargo_app(live="0.547.0"),
        expected_revision="0.547.0",
        kargo={
            "stage": _stage(last=_promotion("0.547.0", "Succeeded"), upstream=["dev"]),
            "freights": [_freight("0.547.0", approved=["prod"], current=["prod"])],
        },
    )
    assert result["verdict"] == VERIFIED
    assert result["kargo"]["expected_freight"]["approved_for"] == ["prod"]
    assert result["kargo"]["expected_freight"]["verified_in"] == []


def test_reverted_promotion_is_drift():
    stage = _stage(last=_promotion("0.547.0", "Succeeded"))
    result = verdict(_kargo_app(live="0.546.4"), kargo={"stage": stage, "freights": []})
    assert result["verdict"] == FAILED
    detail = _check(result, "kargo")["detail"]
    assert "promoted 0.547.0" in detail and "runs 0.546.4" in detail


def test_no_drift_while_the_application_is_still_syncing():
    stage = _stage(last=_promotion("0.547.0", "Succeeded"))
    app = _kargo_app(
        live="0.546.4", sync={"status": "OutOfSync", "revisions": ["0.547.0", GIT]}
    )
    result = verdict(app, kargo={"stage": stage, "freights": []})
    assert all(c["name"] != "kargo" for c in result["checks"])


def test_kargo_read_error_is_reported_and_never_changes_the_verdict():
    result = verdict(_kargo_app(), kargo={"error": "HTTP 403"})
    assert result["verdict"] == VERIFIED
    assert result["kargo"]["error"] == "HTTP 403"


def test_missing_stage_is_reported_without_failing():
    result = verdict(
        _kargo_app(), expected_revision="0.546.4", kargo={"stage": None, "freights": []}
    )
    assert result["verdict"] == VERIFIED
    assert "not found" in result["kargo"]["error"]


def test_no_drift_while_a_promotion_rolls_back_to_older_freight():
    # A Kargo rollback to 0.546.4 settles in ArgoCD while the Promotion is
    # still waiting on its later steps; the last Promotion is still 0.547.0.
    stage = _stage(
        last=_promotion("0.547.0", "Succeeded"),
        current=_promotion("0.546.4", "Running", currentStep=3),
    )
    result = verdict(
        _kargo_app(live="0.546.4"),
        expected_revision="0.546.4",
        kargo={"stage": stage, "freights": [_freight("0.546.4")]},
    )
    assert result["verdict"] == VERIFIED
    assert all(c["name"] != "kargo" for c in result["checks"])


def test_failed_promotion_is_in_progress_while_argocd_still_syncs_its_version():
    # argocd-update already asked for 0.547.0, then a wait step timed out;
    # the sync keeps going and may still land.
    stage = _stage(last=_promotion("0.547.0", "Errored", message="step timed out"))
    app = _kargo_app(
        live="0.546.4",
        operationState={
            "phase": "Running",
            "syncResult": {"revisions": ["0.546.4", GIT]},
        },
    )
    app["spec"]["sources"][0]["targetRevision"] = "0.547.0"
    result = verdict(
        app,
        expected_revision="0.547.0",
        kargo={"stage": stage, "freights": [_freight("0.547.0")]},
    )
    assert result["verdict"] == IN_PROGRESS
    check = _check(result, "kargo")
    assert check["state"] == IN_PROGRESS
    assert "step timed out" in check["detail"] and "still syncing" in check["detail"]


def test_failed_promotion_that_never_applied_fails_even_mid_sync():
    stage = _stage(last=_promotion("0.547.0", "Failed", message="no access"))
    app = _kargo_app(
        live="0.546.4",
        operationState={
            "phase": "Running",
            "syncResult": {"revisions": ["0.546.4", GIT]},
        },
    )
    result = verdict(
        app,
        expected_revision="0.547.0",
        kargo={"stage": stage, "freights": [_freight("0.547.0")]},
    )
    assert result["verdict"] == FAILED


def _dev(last=None, current=None):
    dev = _stage(last=last, current=current)
    dev["metadata"]["name"] = "dev"
    return dev


def test_failed_upstream_promotion_fails_the_downstream_stage():
    failed_dev = _promotion("0.547.0", "Failed", message="conformance gate failed")
    failed_dev["name"] = "dev.0.547.0"
    result = verdict(
        _kargo_app(),
        expected_revision="0.547.0",
        kargo={
            "stage": _stage(upstream=["dev"]),
            "upstream": {"dev": _dev(last=failed_dev)},
            "freights": [_freight("0.547.0", alias="brave-otter")],
        },
    )
    assert result["verdict"] == FAILED
    detail = _check(result, "kargo")["detail"]
    assert "dev.0.547.0" in detail and "conformance gate failed" in detail
    assert "promoted to dev again" in detail


def test_upstream_failure_is_not_final_while_upstream_promotes_again():
    failed_dev = _promotion("0.547.0", "Failed", message="gate")
    result = verdict(
        _kargo_app(),
        expected_revision="0.547.0",
        kargo={
            "stage": _stage(upstream=["dev"]),
            "upstream": {
                "dev": _dev(last=failed_dev, current=_promotion("0.547.0", "Running"))
            },
            "freights": [_freight("0.547.0", alias="brave-otter")],
        },
    )
    assert result["verdict"] == IN_PROGRESS
    assert "waits for verification in dev" in _check(result, "kargo")["detail"]


def test_approved_freight_ignores_a_failed_upstream():
    failed_dev = _promotion("0.547.0", "Failed", message="gate")
    result = verdict(
        _kargo_app(),
        expected_revision="0.547.0",
        kargo={
            "stage": _stage(upstream=["dev"]),
            "upstream": {"dev": _dev(last=failed_dev)},
            "freights": [_freight("0.547.0", approved=["prod"])],
        },
    )
    assert result["verdict"] == IN_PROGRESS
    assert "eligible for prod" in _check(result, "kargo")["detail"]
