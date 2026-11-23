"""Worker pool autoscaling in the factory chart: off renders nothing, on renders
one HorizontalPodAutoscaler per pool that scales the WorkerPool to and from zero
on an external metric, and every pool an HPA owns is one Argo CD ignores."""

from __future__ import annotations

import os
import subprocess
import tempfile
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


def _without_autoscaling() -> Path:
    """The deploy values with both pools' autoscaling removed, so a test
    of the chart's behavior starts from the chart's own defaults, whatever the
    deploy values enable. The one test of the deploy values themselves renders
    them as they are."""
    values = yaml.safe_load(_DEPLOY_VALUES.read_text())
    for agent in ("codex", "reviewer"):
        values["agents"][agent].pop("autoscaling", None)
    path = Path(tempfile.mkdtemp()) / "values.yaml"
    path.write_text(yaml.safe_dump(values))
    return path


_BASE_VALUES = _without_autoscaling()


def _render(
    sets: list[str] | None = None, defaults: bool = True
) -> list[dict[str, Any]]:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "factory",
        str(_CHART),
        "--namespace",
        "kagent",
        "--values",
        str(_BASE_VALUES if defaults else _DEPLOY_VALUES),
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


def _reaper_env(docs, name: str) -> str | None:
    reaper = _one(docs, "Deployment", "factory", "factory-reaper")
    (c,) = reaper["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value") for e in c["env"]}
    return env.get(name)


def _reaper_pools(docs) -> str | None:
    return _reaper_env(docs, "FACTORY_REAPER_POOLS")


def _reaper_unbound(docs) -> str | None:
    return _reaper_env(docs, "FACTORY_REAPER_UNBOUND_POOLS")


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


_REVIEWER_ON = [
    "agents.reviewer.autoscaling.enabled=true",
    "agents.reviewer.autoscaling.minReplicas=3",
    "agents.reviewer.autoscaling.spare=2",
    "agents.reviewer.autoscaling.maxReplicas=5",
]


def test_the_reviewer_pool_autoscales_between_its_concurrency_and_its_workers():
    # The reviewer sends to kagent itself, so the connector publishes no
    # pending demand for its pool: the reaper publishes pending 0 for it
    # (FACTORY_REAPER_UNBOUND_POOLS) and the pool never starts below the
    # reviews it runs at once.
    docs = _render(["reviewer.concurrency=3", *_REVIEWER_ON])
    (hpa,) = _hpas(docs)
    ns, name = _REVIEW
    assert hpa["metadata"]["namespace"] == ns and hpa["metadata"]["name"] == name
    assert hpa["spec"]["minReplicas"] == 3
    assert hpa["spec"]["maxReplicas"] == 5
    assert hpa["spec"]["scaleTargetRef"]["name"] == name
    assert _reaper_pools(docs) == f"{ns}/{name}=2"
    assert _reaper_unbound(docs) == f"{ns}/{name}"
    # The reaper patches pods only in the namespace of an autoscaled pool.
    assert _reaper_verbs(docs, ns) == ["get", "list", "watch", "delete", "patch"]
    assert _reaper_verbs(docs, _CODER[0]) == ["get", "list", "watch", "delete"]


def test_only_the_reviewer_pool_is_unbound():
    # The connector publishes pending for the coder pool, and a second 0 from
    # the reaper would hide a failed connector read.
    docs = _render(["agents.codex.autoscaling.enabled=true", *_REVIEWER_ON])
    assert _reaper_pools(docs) == (
        f"{_CODER[0]}/{_CODER[1]}=2,{_REVIEW[0]}/{_REVIEW[1]}=2"
    )
    assert _reaper_unbound(docs) == f"{_REVIEW[0]}/{_REVIEW[1]}"
    coder_only = _render(["agents.codex.autoscaling.enabled=true"])
    assert _reaper_unbound(coder_only) is None


def test_the_reviewer_pool_minimum_covers_its_concurrency():
    # A burst of reviews cannot wait for a scale-up the reviewer cannot ask for.
    for min_replicas in ("0", "2"):
        with pytest.raises(RuntimeError, match="at least reviewer.concurrency"):
            _render(
                [
                    "reviewer.concurrency=3",
                    "agents.reviewer.autoscaling.enabled=true",
                    f"agents.reviewer.autoscaling.minReplicas={min_replicas}",
                ]
            )
    # Without a minimum the chart's default is 0: refused.
    with pytest.raises(RuntimeError, match="at least reviewer.concurrency"):
        _render(["agents.reviewer.autoscaling.enabled=true"])
    _render(["reviewer.concurrency=3", *_REVIEWER_ON])
    # A larger reviewer concurrency raises the floor.
    with pytest.raises(RuntimeError, match=r"concurrency \(4\), got 3"):
        _render(["reviewer.concurrency=4", *_REVIEWER_ON])


def test_the_reviewer_pool_needs_a_spare():
    # With no spare the pool never exceeds its actors, so the goal check
    # session that follows a review would find no free worker.
    with pytest.raises(RuntimeError, match="spare of at least 1"):
        _render(
            [
                "reviewer.concurrency=3",
                "agents.reviewer.autoscaling.enabled=true",
                "agents.reviewer.autoscaling.minReplicas=3",
                "agents.reviewer.autoscaling.spare=0",
            ]
        )


def test_reviewer_autoscaling_off_changes_nothing():
    off = _render(["agents.reviewer.autoscaling.minReplicas=0"])
    assert _hpas(off) == []
    assert _reaper_pools(off) is None and _reaper_unbound(off) is None


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
    assert _unignored(_render(defaults=False), app) == set()


def test_the_ignore_check_catches_a_pool_with_no_entry():
    app = yaml.safe_load(_APPLICATION.read_text())
    # The deploy values' own entry would hide the gap: start without entries.
    app["spec"]["ignoreDifferences"] = [
        d for d in app["spec"]["ignoreDifferences"] if d.get("group") != "ate.dev"
    ]
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


def test_deploy_values_let_the_coder_pool_scale_to_zero_behind_a_send_wait():
    # The coder pool follows demand between 0 and 12 workers (12 is the fixed
    # pool's size). A pool at zero needs the connector's send wait, which holds
    # a send until a worker is ready: without it the send fails with "no worker
    # has room". The wait must cover a cold start: Prometheus scrape (30 s),
    # HPA sync (15 s), pod schedule and start, reaper row (15 s), and one slow
    # schedule seen on 2026-11-20 (161 s), so it is at least 3 min.
    docs = _render(defaults=False)
    hpas = {h["metadata"]["name"]: h for h in _hpas(docs)}
    assert set(hpas) == {_CODER[1], _REVIEW[1]}
    hpa = hpas[_CODER[1]]
    assert hpa["spec"]["minReplicas"] == 0
    assert (
        hpa["spec"]["maxReplicas"]
        == _one(docs, "WorkerPool", *_CODER)["spec"]["replicas"]
    )
    assert _reaper_pools(docs) == (
        f"{_CODER[0]}/{_CODER[1]}=2,{_REVIEW[0]}/{_REVIEW[1]}=2"
    )
    cm = _one(docs, "ConfigMap", "factory", "factory-connector")
    wait = yaml.safe_load(cm["data"]["config.yaml"])["intervals"]["workerWait"]
    assert wait == "5m"
    # A held send is not issued, so no task lease runs on it. A bound above
    # 5 min would only delay the failure of a pool that cannot start.
    assert 3 * 60 <= _seconds(wait) <= 5 * 60


def test_deploy_values_let_the_reviewer_pool_follow_its_reviews():
    # The reviewer pool holds its concurrency (3) at least and never more than
    # the 5 workers it ran fixed. Only it is "unbound": the connector
    # publishes the coder pool's pending series, and a second 0 from the reaper
    # would hide a failed connector read.
    docs = _render(defaults=False)
    hpa = _one(docs, "HorizontalPodAutoscaler", *_REVIEW)["spec"]
    pool = _one(docs, "WorkerPool", *_REVIEW)
    assert hpa["minReplicas"] == 3
    assert hpa["maxReplicas"] == 5 == pool["spec"]["replicas"]
    assert _reaper_unbound(docs) == f"{_REVIEW[0]}/{_REVIEW[1]}"
    # The reaper may patch pods (the deletion cost) in the review namespace.
    assert _reaper_verbs(docs, _REVIEW[0]) == ["get", "list", "watch", "delete", "patch"]
    # The values and the chart's concurrency floor agree.
    values = yaml.safe_load(_DEPLOY_VALUES.read_text())
    assert hpa["minReplicas"] >= values["reviewer"]["concurrency"]


def test_the_reviewer_pool_needs_room_to_grow():
    # With maxReplicas at minReplicas the spare never starts.
    for max_replicas in ("3", "2"):
        with pytest.raises(RuntimeError, match="maxReplicas above minReplicas"):
            _render(
                [
                    "reviewer.concurrency=3",
                    "agents.reviewer.autoscaling.enabled=true",
                    "agents.reviewer.autoscaling.minReplicas=3",
                    f"agents.reviewer.autoscaling.maxReplicas={max_replicas}",
                ]
            )
    _render(
        [
            "reviewer.concurrency=3",
            "agents.reviewer.autoscaling.enabled=true",
            "agents.reviewer.autoscaling.minReplicas=3",
            "agents.reviewer.autoscaling.maxReplicas=4",
        ]
    )


def _render_with_trial(sets: list[str]) -> subprocess.CompletedProcess:
    """The deploy values with the coder trial's arms."""
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "factory",
        str(_CHART),
        "--namespace",
        "kagent",
        "--values",
        str(_DEPLOY_VALUES),
        "--values",
        str(_CHART / "files" / "coder-trial-values.yaml"),
    ]
    for s in sets:
        argv += ["--set", s]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def test_a_binding_with_a_non_control_arm_cannot_scale_to_zero():
    # A non-control arm stores its sends under the Agent homelab-codex-<arm>.
    # The send wait reads the capacity row of that pool (none: it sends at
    # once) and the pending series names it, while the HPA scales
    # homelab-codex only. At zero such a send has no wake signal and no hold.
    run = _render_with_trial(["agents.codex.autoscaling.minReplicas=0"])
    assert run.returncode != 0
    # The message names the binding and the arm's Agent, not "<nil>".
    assert "binding homelab has the arm" in run.stderr, run.stderr
    assert "Agent homelab-codex-astra-high" in run.stderr, run.stderr
    assert "cannot scale to zero" in run.stderr
    # One worker or more is safe: the pool is never empty.
    ok = _render_with_trial(["agents.codex.autoscaling.minReplicas=1"])
    assert ok.returncode == 0, ok.stderr


def _seconds(duration: str) -> int:
    """A Go duration of whole minutes or seconds, in seconds."""
    unit = {"s": 1, "m": 60}[duration[-1]]
    return int(duration[:-1]) * unit
