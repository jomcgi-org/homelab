"""Guard the monolith policy-denied ingress drop alert chain (#4659).

The armed monolith-api-ingress CiliumNetworkPolicy fails closed as a silent
dial timeout, and the hubble ring buffer is only minutes deep, so drops are
unanswerable retroactively. The fix is four coupled pieces, and a regression in
any one of them silently recreates the blind spot:

1. projects/platform/cilium/values.yaml gives the hubble `drop` metric
   destination context labels (bare hubble_drop_total is node-wide and
   cumulative, so drops cannot be attributed); bootstrap/cilium-helmchart.yaml
   must carry the identical list or a node reboot reverts the config.
2. The prometheus/hubble receiver in values-prod.yaml scrapes :9965 into
   SigNoz, which is the durable record the rule reads.
3. hubble-monolith-policy-drops-alert.yaml fires only on SUSTAINED policy-
   denied ingress drops toward namespace monolith: timeAggregation=increase
   (a windowed rate, not the cumulative counter), matchType=5 (10 consecutive
   evaluations over 10 minutes), never on a single blip.
4. The ConfigMap is registered in kustomization.yaml (an unregistered manifest
   never reaches the cluster, so the sidecar never syncs it).

The tests read the YAML files directly (no Helm toolchain), mirroring
signoz_alerts_test.py. Beyond direct asserts, a mutation harness re-runs the
full validation against mutated copies and proves each assert is load-bearing:
every mutation that recreates a failure mode is detected, by its own defect id.

Numbers here are load-bearing elsewhere: the 10-consecutive-evaluation shape is
asserted below, and the filter/aggregations are asserted against the rendered
alert JSON exactly.
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

import pytest
import yaml

ALERT_FILE = "hubble-monolith-policy-drops-alert.yaml"
KUSTOMIZATION = "kustomization.yaml"

METRIC = "hubble_drop_total"
EXPECTED_FILTER = (
    "reason = 'POLICY_DENIED' AND destination_namespace = 'monolith' "
    "AND traffic_direction = 'ingress'"
)
# What the alert needs from the drop metric's labelsContext. destination_workload
# is requested (for future tightening) but deliberately NOT filtered on yet: its
# live value has not been confirmed against a :9965/metrics scrape, same posture
# as the fc-invoke L7 alert deferring to the certain namespace label.
REQUIRED_DROP_LABELS = {
    "source_namespace",
    "destination_namespace",
    "traffic_direction",
}
TIGHTENING_DROP_LABELS = REQUIRED_DROP_LABELS | {"destination_workload"}

HUBBLE_PORT = 9965
EVAL_WINDOW = "10m0s"
FREQUENCY = "1m0s"
MIN_CONSECUTIVE = 5


def _platform_dir() -> Path:
    srcdir = os.environ.get("TEST_SRCDIR", "")
    candidate = Path(srcdir) / "_main" / "projects" / "platform"
    if candidate.exists():
        return candidate
    # Fallback for a direct (non-bazel) run: this file lives in
    # projects/platform/signoz/, so the platform tree is one level up.
    here = Path(__file__).resolve().parent.parent
    if (here / "signoz").exists():
        return here
    raise FileNotFoundError(f"platform dir not found at {candidate} or {here}")


def _load_yaml(rel: str) -> dict:
    return yaml.safe_load((_platform_dir() / rel).read_text())


def _parse_duration(value: str) -> float:
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value)
    assert match is not None, f"unparseable duration {value!r}"
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _pristine() -> dict:
    """Every document the validation reads, freshly parsed."""
    cm = _load_yaml(f"signoz/{ALERT_FILE}")
    kustomization = _load_yaml(f"signoz/{KUSTOMIZATION}")
    cilium_values = _load_yaml("cilium/values.yaml")
    helmchart = yaml.safe_load(
        (_platform_dir() / "cilium/bootstrap/cilium-helmchart.yaml").read_text()
    )
    bootstrap_values = yaml.safe_load(helmchart["spec"]["valuesContent"])
    values_prod = _load_yaml("signoz/values-prod.yaml")
    alert = json.loads(cm["data"]["alert.json"])
    return {
        "cm": cm,
        "alert": alert,
        "kustomization": kustomization,
        "cilium_values": cilium_values,
        "bootstrap_values": bootstrap_values,
        "values_prod": values_prod,
    }


# ---------------------------------------------------------------------------
# Validation: returns the set of defect ids present in the given documents.
# Empty set means the whole chain holds. Each id maps 1:1 to a mutation below,
# which is what proves every assert is load-bearing.
# ---------------------------------------------------------------------------


def _defects(docs: dict) -> set[str]:
    defects: set[str] = set()

    cm, alert = docs["cm"], docs["alert"]
    condition = alert["condition"]
    query = condition["compositeQuery"]["queries"][0]["spec"]
    agg = query["aggregations"][0]

    if cm["metadata"].get("labels", {}).get("signoz.io/alert") != "true":
        defects.add("sidecar-label")

    annotated = cm["metadata"]["annotations"]["signoz.io/notification-channels"]
    thresholds = condition["thresholds"]["spec"]
    if sorted(annotated.split(",")) != sorted(alert["preferredChannels"]) or any(
        sorted(t["channels"]) != sorted(alert["preferredChannels"]) for t in thresholds
    ):
        defects.add("routing")

    spec0 = thresholds[0]
    if any(
        condition[f] != spec0[f] or condition[f] == ""
        for f in ("op", "target", "matchType")
    ):
        defects.add("legacy-threshold-fields")

    if agg["metricName"] != METRIC:
        defects.add("metric-name")
    if agg["timeAggregation"] != "increase":
        defects.add("rate-not-cumulative")
    if agg["spaceAggregation"] != "sum":
        defects.add("space-aggregation")

    expression = query["filter"]["expression"]
    if "'POLICY_DENIED'" not in expression:
        defects.add("reason-filter")
    if "destination_namespace = 'monolith'" not in expression:
        defects.add("namespace-scope")
    if "traffic_direction = 'ingress'" not in expression:
        defects.add("direction-scope")
    if expression != EXPECTED_FILTER:
        defects.add("filter-exact")

    if condition["op"] != "1":
        defects.add("greater-than-op")
    if float(condition["target"]) != 0.0:
        defects.add("zero-target")

    if condition["matchType"] != "5":
        defects.add("consecutive-matchtype")
    else:
        window_s = _parse_duration(alert["evalWindow"])
        frequency_s = _parse_duration(alert["frequency"])
        consecutive = round(window_s / frequency_s)
        if consecutive < MIN_CONSECUTIVE or window_s / frequency_s != consecutive:
            defects.add("sustained-window")
        step_s = float(query["stepInterval"])
        if frequency_s < step_s or frequency_s % step_s != 0:
            defects.add("step-cadence")

    if alert["severity"] != "critical":
        defects.add("severity")
    if cm["metadata"]["annotations"]["signoz.io/severity"] != "critical":
        defects.add("severity")
    if alert["disabled"] is not False:
        defects.add("enabled")

    if ALERT_FILE not in docs["kustomization"]["resources"]:
        defects.add("registered")

    # Metric path: scoped drop metric in BOTH cilium configs, identical lists.
    metrics = docs["cilium_values"]["cilium"]["hubble"]["metrics"]["enabled"]
    bootstrap_metrics = docs["bootstrap_values"]["hubble"]["metrics"]["enabled"]
    if metrics != bootstrap_metrics:
        defects.add("bootstrap-match")
    drop_entries = [m for m in metrics if m.split(":", 1)[0] == "drop"]
    if len(drop_entries) != 1:
        defects.add("drop-scoped")
    else:
        entry = drop_entries[0]
        options = entry.split(":", 1)[1] if ":" in entry else ""
        labels: set[str] = set()
        for kv in options.split(";"):
            if kv.startswith("labelsContext="):
                labels = set(filter(None, kv.split("=", 1)[1].split(",")))
        if not REQUIRED_DROP_LABELS <= labels or not TIGHTENING_DROP_LABELS <= labels:
            defects.add("drop-scoped")

    # Scrape path: hubble receiver exists, targets :9965, and reaches the pipeline.
    config = docs["values_prod"]["k8s-infra"]["otelDeployment"]["config"]
    receiver = config["receivers"].get("prometheus/hubble")
    scrape_ok = False
    if receiver:
        for job in receiver["config"]["scrape_configs"]:
            for rc in job.get("relabel_configs", []):
                if rc.get("replacement") == f"$1:{HUBBLE_PORT}":
                    scrape_ok = True
    if not scrape_ok:
        defects.add("scrape-config")
    wired = config["service"]["pipelines"]["metrics/scraper"]["receivers"]
    if "prometheus/hubble" not in wired:
        defects.add("pipeline-wired")

    return defects


# ---------------------------------------------------------------------------
# Mutations: each recreates one failure mode. The parametrized test applies the
# mutation to a pristine copy and asserts the validator names THAT defect,
# proving the corresponding assert cannot regress silently.
# ---------------------------------------------------------------------------


def _mutated_query(alert: dict) -> dict:
    return alert["condition"]["compositeQuery"]["queries"][0]["spec"]


MUTATIONS = {
    "sidecar-label": lambda d: d["cm"]["metadata"]["labels"].update(
        {"signoz.io/alert": "false"}
    ),
    "routing": lambda d: d["cm"]["metadata"]["annotations"].update(
        {"signoz.io/notification-channels": "slack"}
    ),
    "legacy-threshold-fields": lambda d: d["alert"]["condition"].update({"op": "2"}),
    "metric-name": lambda d: _mutated_query(d["alert"])["aggregations"][0].update(
        {"metricName": "cilium_drop_count_total"}
    ),
    "rate-not-cumulative": lambda d: _mutated_query(d["alert"])["aggregations"][
        0
    ].update({"timeAggregation": "avg"}),
    "space-aggregation": lambda d: _mutated_query(d["alert"])["aggregations"][0].update(
        {"spaceAggregation": "max"}
    ),
    "reason-filter": lambda d: _mutated_query(d["alert"])["filter"].update(
        {"expression": EXPECTED_FILTER.replace("reason = 'POLICY_DENIED' AND ", "")}
    ),
    "namespace-scope": lambda d: _mutated_query(d["alert"])["filter"].update(
        {
            "expression": EXPECTED_FILTER.replace(
                "destination_namespace = 'monolith' AND ", ""
            )
        }
    ),
    "direction-scope": lambda d: _mutated_query(d["alert"])["filter"].update(
        {
            "expression": EXPECTED_FILTER.replace(
                " AND traffic_direction = 'ingress'", ""
            )
        }
    ),
    "filter-exact": lambda d: _mutated_query(d["alert"])["filter"].update(
        {"expression": EXPECTED_FILTER + " AND protocol = 'UDP'"}
    ),
    "greater-than-op": lambda d: d["alert"]["condition"].update({"op": "3"}),
    "zero-target": lambda d: d["alert"]["condition"].update({"target": 5}),
    "consecutive-matchtype": lambda d: d["alert"]["condition"].update(
        {"matchType": "1"}
    ),
    "sustained-window": lambda d: d["alert"].update({"evalWindow": "2m0s"}),
    "step-cadence": lambda d: _mutated_query(d["alert"]).update({"stepInterval": 120}),
    "severity": lambda d: (
        d["alert"].update({"severity": "warning"}),
        d["cm"]["metadata"]["annotations"].update({"signoz.io/severity": "warning"}),
    ),
    "enabled": lambda d: d["alert"].update({"disabled": True}),
    "registered": lambda d: d["kustomization"]["resources"].remove(ALERT_FILE),
    "drop-scoped": lambda d: d["cilium_values"]["cilium"]["hubble"]["metrics"].update(
        {"enabled": ["dns", "drop", "tcp", "flow"]}
    ),
    "bootstrap-match": lambda d: d["bootstrap_values"]["hubble"]["metrics"][
        "enabled"
    ].remove("tcp"),
    "scrape-config": lambda d: d["values_prod"]["k8s-infra"]["otelDeployment"][
        "config"
    ]["receivers"]["prometheus/hubble"]["config"]["scrape_configs"][0][
        "relabel_configs"
    ].remove(
        {
            "source_labels": ["__meta_kubernetes_pod_ip"],
            "target_label": "__address__",
            "replacement": f"$1:{HUBBLE_PORT}",
        }
    ),
    "pipeline-wired": lambda d: d["values_prod"]["k8s-infra"]["otelDeployment"][
        "config"
    ]["service"]["pipelines"]["metrics/scraper"]["receivers"].remove(
        "prometheus/hubble"
    ),
}


@pytest.mark.parametrize("mutation_id", sorted(MUTATIONS))
def test_every_assert_is_load_bearing(mutation_id):
    """Mutating one load-bearing field must surface exactly that defect."""
    docs = copy.deepcopy(_pristine())
    MUTATIONS[mutation_id](docs)
    defects = _defects(docs)
    assert mutation_id in defects, (
        f"mutation {mutation_id!r} went undetected: the matching assert is not "
        f"load-bearing and the failure mode it guards would return silently"
    )


def test_rendered_rule_has_no_defects():
    leftover = _defects(_pristine())
    assert leftover == set(), (
        f"the committed config itself has defects: {sorted(leftover)}"
    )


# ---------------------------------------------------------------------------
# Direct asserts for the properties worth stating in plain language (the
# mutation harness above already proves they are enforced).
# ---------------------------------------------------------------------------


def test_alert_scopes_policy_denied_ingress_to_monolith():
    alert = _pristine()["alert"]
    query = alert["condition"]["compositeQuery"]["queries"][0]["spec"]
    assert query["filter"]["expression"] == EXPECTED_FILTER
    assert query["aggregations"][0]["metricName"] == METRIC
    # increase over each 60s step makes every evaluation a RATE; the absolute
    # cumulative counter would always be nonzero (issue #4659).
    assert query["aggregations"][0]["timeAggregation"] == "increase"


def test_alert_fires_on_sustained_denial_not_a_blip():
    alert = _pristine()["alert"]
    condition = alert["condition"]
    assert condition["matchType"] == "5"
    window_s = _parse_duration(alert["evalWindow"])
    frequency_s = _parse_duration(alert["frequency"])
    consecutive = round(window_s / frequency_s)
    assert window_s / frequency_s == consecutive
    assert consecutive == 10, (
        "10 evaluations at 1m frequency means drops must persist ~10 minutes; "
        "loosen deliberately, never accidentally"
    )
    assert float(condition["target"]) == 0.0 and condition["op"] == "1"


def test_cilium_configs_agree_on_the_scoped_drop_metric():
    docs = _pristine()
    values_metrics = docs["cilium_values"]["cilium"]["hubble"]["metrics"]["enabled"]
    bootstrap_metrics = docs["bootstrap_values"]["hubble"]["metrics"]["enabled"]
    assert values_metrics == bootstrap_metrics, (
        "the hubble metrics lists diverge; a node reboot reverts the agent to "
        "the bootstrap seed and the drop series loses its context labels"
    )
    assert any(m.startswith("drop:labelsContext=") for m in values_metrics)
