"""Worker pool autoscaling in the factory chart: off renders nothing, on renders
one HorizontalPodAutoscaler per pool that scales the WorkerPool to and from zero
on an external metric, and every pool an HPA owns is one Argo CD ignores."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART = Path(__file__).resolve().parent
_DEPLOY_VALUES = Path(
    os.environ.get("DEPLOY_VALUES", _CHART.parent / "deploy" / "values.yaml")
)
_APPLICATION = Path(
    os.environ.get("APPLICATION", _CHART.parent / "deploy" / "application.yaml")
)
_CODER = ("factory-r-homelab", "homelab-codex")
_REVIEW = ("factory-r-homelab-review", "homelab-reviewer")


def _render(sets: list[str] | None = None) -> list[dict[str, Any]]:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "factory",
        str(_CHART),
        "--namespace",
        "kagent",
        "--values",
        str(_DEPLOY_VALUES),
    ]
    for s in sets or []:
        argv += ["--set", s]
    run = subprocess.run(argv, capture_output=True, text=True, check=False)
    if run.returncode != 0:
        raise RuntimeError(run.stderr)
    return [d for d in yaml.safe_load_all(run.stdout) if d]


def _one(docs, kind, ns, name):
    found = [
        d
        for d in docs
        if d["kind"] == kind
        and d["metadata"]["name"] == name
        and d["metadata"].get("namespace") == ns
    ]
    assert len(found) == 1, f"expected one {kind} {ns}/{name}, found {len(found)}"
    return found[0]


def _hpas(docs):
    return [d for d in docs if d["kind"] == "HorizontalPodAutoscaler"]


def _reaper_pools(docs) -> str | None:
    reaper = _one(docs, "Deployment", "factory", "factory-reaper")
    (c,) = reaper["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value") for e in c["env"]}
    return env.get("FACTORY_REAPER_POOLS")


def test_off_by_default_renders_no_autoscaler_and_no_reaper_pools():
    docs = _render()
    assert _hpas(docs) == []
    assert _reaper_pools(docs) is None
    for ns, name in (_CODER, _REVIEW):
        pool = _one(docs, "WorkerPool", ns, name)
        assert isinstance(pool["spec"]["replicas"], int)


def test_enabled_pool_gets_an_hpa_that_scales_the_workerpool_to_zero():
    docs = _render(["agents.codex.autoscaling.enabled=true"])
    (hpa,) = _hpas(docs)
    ns, name = _CODER
    pool = _one(docs, "WorkerPool", ns, name)
    assert hpa["apiVersion"] == "autoscaling/v2"
    assert hpa["metadata"]["namespace"] == ns and hpa["metadata"]["name"] == name
    spec = hpa["spec"]
    assert spec["scaleTargetRef"] == {
        "apiVersion": "ate.dev/v1",
        "kind": "WorkerPool",
        "name": name,
    }
    assert spec["minReplicas"] == 0
    # concurrency + the default spare of 2.
    assert spec["maxReplicas"] == pool["spec"]["replicas"] + 2
    # Kubernetes rejects minReplicas 0 without an Object or External metric.
    assert spec["metrics"] == [
        {
            "type": "External",
            "external": {
                "metric": {
                    "name": "factory_worker_pool_workers_wanted",
                    "selector": {"matchLabels": {"pool_namespace": ns, "pool": name}},
                },
                "target": {"type": "AverageValue", "averageValue": "1"},
            },
        }
    ]
    up = spec["behavior"]["scaleUp"]
    # The HPA's default 10% tolerance would hide a one-worker gap above about
    # 10 replicas at AverageValue 1.
    assert up["tolerance"] == "0" and spec["behavior"]["scaleDown"]["tolerance"] == "0"
    assert up["stabilizationWindowSeconds"] == 0
    assert up["policies"] == [
        {"type": "Pods", "value": spec["maxReplicas"], "periodSeconds": 15}
    ]
    down = spec["behavior"]["scaleDown"]
    assert down["stabilizationWindowSeconds"] == 1800
    assert down["policies"] == [{"type": "Pods", "value": 1, "periodSeconds": 300}]
    # The other pool is untouched.
    assert [h["metadata"]["name"] for h in _hpas(docs)] == [name]
    assert _reaper_pools(docs) == f"{ns}/{name}=2"


def test_overrides():
    docs = _render(
        [
            "agents.codex.autoscaling.enabled=true",
            "agents.codex.autoscaling.minReplicas=1",
            "agents.codex.autoscaling.spare=0",
            "agents.codex.autoscaling.scaleDown.stabilizationSeconds=600",
        ]
    )
    (hpa,) = _hpas(docs)
    coder = hpa["spec"]
    assert coder["minReplicas"] == 1
    pool = _one(docs, "WorkerPool", *_CODER)
    assert coder["maxReplicas"] == pool["spec"]["replicas"]
    assert coder["behavior"]["scaleDown"]["stabilizationWindowSeconds"] == 600
    assert _reaper_pools(docs) == f"{_CODER[0]}/{_CODER[1]}=0"


def _reaper_verbs(docs, ns):
    role = _one(docs, "Role", ns, "factory-reaper")
    (rule,) = role["rules"]
    return rule["verbs"]


def test_reaper_may_patch_pods_only_where_a_pool_autoscales():
    # The deletion cost makes a scale-down remove idle workers first.
    off = _render()
    assert _reaper_verbs(off, _CODER[0]) == ["get", "list", "watch", "delete"]
    assert _reaper_verbs(off, _REVIEW[0]) == ["get", "list", "watch", "delete"]
    on = _render(["agents.codex.autoscaling.enabled=true"])
    assert _reaper_verbs(on, _CODER[0]) == ["get", "list", "watch", "delete", "patch"]
    assert _reaper_verbs(on, _REVIEW[0]) == ["get", "list", "watch", "delete"]


def test_the_reviewer_pool_cannot_be_enabled():
    # The reviewer sends to kagent itself, not through the connector, so no
    # pending demand series exists for its pool and the metric never has a
    # value. Refuse it, at any minReplicas.
    for min_replicas in ("0", "1"):
        with pytest.raises(RuntimeError, match="reviewer pool cannot autoscale"):
            _render(
                [
                    "agents.reviewer.autoscaling.enabled=true",
                    f"agents.reviewer.autoscaling.minReplicas={min_replicas}",
                ]
            )
    # Off, nothing changes.
    assert _hpas(_render(["agents.reviewer.autoscaling.minReplicas=0"])) == []


def test_scale_down_period_stays_within_the_hpa_maximum():
    # autoscaling/v2 allows a policy period of at most 1800 s.
    _render(["agents.codex.autoscaling.scaleDown.periodSeconds=1800"])
    with pytest.raises(RuntimeError, match="periodSeconds"):
        _render(["agents.codex.autoscaling.scaleDown.periodSeconds=1801"])


def test_minimum_above_maximum_is_refused():
    with pytest.raises(RuntimeError, match="minReplicas 5 is above maxReplicas 3"):
        _render(
            [
                "agents.codex.autoscaling.enabled=true",
                "agents.codex.autoscaling.minReplicas=5",
                "agents.codex.autoscaling.maxReplicas=3",
            ]
        )


def test_schema_refuses_unknown_autoscaling_keys():
    with pytest.raises(RuntimeError, match="autoscaling"):
        _render(["agents.codex.autoscaling.surprise=1"])


def test_worker_wait_is_a_connector_interval():
    # --set-string keeps "5m" a string; the schema must accept the key.
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "factory",
        str(_CHART),
        "--namespace",
        "kagent",
        "--values",
        str(_DEPLOY_VALUES),
        "--set-string",
        "connector.intervals.workerWait=5m",
    ]
    run = subprocess.run(argv, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def _unignored(docs, app):
    """The pools an HPA scales that Argo CD does not ignore."""
    ignored = {
        (d.get("namespace"), d.get("name"))
        for d in app["spec"].get("ignoreDifferences", [])
        if d.get("group") == "ate.dev"
        and d.get("kind") == "WorkerPool"
        and "/spec/replicas" in d.get("jsonPointers", [])
    }
    scaled = {
        (h["metadata"]["namespace"], h["spec"]["scaleTargetRef"]["name"])
        for h in _hpas(docs)
        if h["spec"]["scaleTargetRef"]["kind"] == "WorkerPool"
    }
    return scaled - ignored


def test_every_pool_an_hpa_scales_is_ignored_by_argo():
    # An HPA changes spec.replicas, and a sync with selfHeal would put it back
    # (ServerSideApply forces the field). RespectIgnoreDifferences leaves it.
    # The check runs on the real render, so a chart default counts too.
    app = yaml.safe_load(_APPLICATION.read_text())
    assert "RespectIgnoreDifferences=true" in app["spec"]["syncPolicy"]["syncOptions"]
    assert _unignored(_render(), app) == set()


def test_the_ignore_check_catches_a_pool_with_no_entry():
    app = yaml.safe_load(_APPLICATION.read_text())
    on = _render(["agents.codex.autoscaling.enabled=true"])
    assert _unignored(on, app) == {_CODER}
    # An entry for another kind or group is not enough.
    app["spec"]["ignoreDifferences"] = [
        {
            "group": "apps",
            "kind": "WorkerPool",
            "namespace": _CODER[0],
            "name": _CODER[1],
            "jsonPointers": ["/spec/replicas"],
        }
    ]
    assert _unignored(on, app) == {_CODER}
    app["spec"]["ignoreDifferences"][0]["group"] = "ate.dev"
    assert _unignored(on, app) == set()
