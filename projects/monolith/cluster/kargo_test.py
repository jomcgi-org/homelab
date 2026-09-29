"""plan_promotion: re-promote only what Kargo would already let the Stage take."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cluster.kargo import PromotionRefused, plan_promotion

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
REPO = "oci://ghcr.io/jomcgi/homelab/charts/embervm"


def _stage(sources=None, current=None, steps=None, vars_=None):
    template = {"steps": [{"uses": "argocd-update"}] if steps is None else steps}
    if vars_:
        template["vars"] = vars_
    return {
        "metadata": {"name": "prod"},
        "spec": {
            "requestedFreight": [
                {"sources": sources if sources is not None else {"stages": ["dev"]}}
            ],
            "promotionTemplate": {"spec": template},
        },
        "status": {"currentPromotion": current} if current else {},
    }


def _freight(version="0.6.0", verified=None, approved=(), current=None, name="f1"):
    return {
        "metadata": {"name": name},
        "alias": f"alias-{name}",
        "charts": [{"repoURL": REPO, "version": version}],
        "status": {
            "verifiedIn": verified if verified is not None else {"dev": {}},
            "approvedFor": {s: {} for s in approved},
            "currentlyIn": current or {},
        },
    }


def _plan(stage, freights, version="0.6.0"):
    return plan_promotion(
        stage,
        freights,
        namespace="kargo-embervm",
        stage_name="prod",
        chart="embervm",
        version=version,
        now=NOW,
    )


def test_plans_the_stage_template_for_the_exact_version():
    body = _plan(
        _stage(vars_=[{"name": "x", "value": "1"}]),
        [_freight("0.6.1", name="newer"), _freight("0.6.0", name="f1")],
    )
    assert body == {
        "apiVersion": "kargo.akuity.io/v1alpha1",
        "kind": "Promotion",
        "metadata": {"generateName": "prod.", "namespace": "kargo-embervm"},
        "spec": {
            "stage": "prod",
            "freight": "f1",
            "steps": [{"uses": "argocd-update"}],
            "vars": [{"name": "x", "value": "1"}],
        },
    }


def test_refuses_while_a_promotion_runs():
    with pytest.raises(PromotionRefused, match="prod.abc is still running"):
        _plan(_stage(current={"name": "prod.abc"}), [_freight()])


def test_refuses_unknown_version_and_missing_stage():
    with pytest.raises(PromotionRefused, match="no Freight for chart embervm 0.7.0"):
        _plan(_stage(), [_freight()], version="0.7.0")
    with pytest.raises(PromotionRefused, match="not found"):
        _plan(None, [_freight()])


def test_refuses_freight_not_verified_upstream_and_never_approves():
    with pytest.raises(PromotionRefused, match="not verified in dev.*Kargo UI"):
        _plan(_stage(), [_freight(verified={})])


def test_manual_approval_makes_freight_available():
    body = _plan(_stage(), [_freight(verified={}, approved=["prod"])])
    assert body["spec"]["freight"] == "f1"


def test_direct_sources_need_no_verification():
    body = _plan(_stage(sources={"direct": True}), [_freight(verified={})])
    assert body["spec"]["freight"] == "f1"


def test_soak_time_is_enforced_from_current_use_or_longest_soak():
    stage = _stage(sources={"stages": ["dev"], "requiredSoakTime": "1h30m0s"})
    young = _freight(
        current={"dev": {"since": (NOW - timedelta(minutes=10)).isoformat()}}
    )
    with pytest.raises(PromotionRefused, match="soaked for 1h30m0s in dev"):
        _plan(stage, [young])
    old = _freight(current={"dev": {"since": (NOW - timedelta(hours=2)).isoformat()}})
    assert _plan(stage, [old])["spec"]["freight"] == "f1"
    left = _freight(verified={"dev": {"longestSoak": "2h0m0s"}})
    assert _plan(stage, [left])["spec"]["freight"] == "f1"


def test_all_strategy_needs_every_upstream():
    stage = _stage(sources={"stages": ["dev", "qa"], "availabilityStrategy": "All"})
    with pytest.raises(PromotionRefused, match="not verified in qa"):
        _plan(stage, [_freight(verified={"dev": {}})])
    one_of = _stage(sources={"stages": ["dev", "qa"]})
    assert _plan(one_of, [_freight(verified={"dev": {}})])["spec"]["freight"] == "f1"


def test_refuses_a_stage_without_steps_and_an_unreadable_soak():
    with pytest.raises(PromotionRefused, match="no promotion steps"):
        _plan(_stage(steps=[]), [_freight()])
    bad = _stage(sources={"stages": ["dev"], "requiredSoakTime": "soon"})
    with pytest.raises(PromotionRefused, match="cannot read soak time"):
        _plan(bad, [_freight()])
