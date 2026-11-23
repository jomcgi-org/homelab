"""The adapter serves one external metric, and its query adds up as designed.

promtool evaluates the real metricsQuery from values.yaml against sample
series, so a change to the query or to a source label fails here.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
VALUES = yaml.safe_load((HERE / "values.yaml").read_text())
POLICY = yaml.safe_load((HERE / "resources/network-policy.yaml").read_text())
APPLICATION = yaml.safe_load((HERE / "application.yaml").read_text())
RULE = VALUES["rules"]["external"][0]
POOL = {"pool_namespace": "factory-r-homelab", "pool": "homelab-codex"}


def test_one_external_rule_and_no_resource_or_custom_metrics():
    assert VALUES["rules"]["default"] is False
    assert len(VALUES["rules"]["external"]) == 1
    assert not VALUES["rules"].get("custom")
    assert RULE["name"]["as"] == "factory_worker_pool_workers_wanted"
    # The HPA lives in the pool's namespace; the series name it in labels.
    assert RULE["resources"] == {"namespaced": False}


def test_the_chart_is_pinned_and_reads_prometheus_in_its_namespace():
    source = APPLICATION["spec"]["sources"][0]
    assert source["chart"] == "prometheus-adapter"
    assert source["targetRevision"] == "5.4.2"
    assert VALUES["prometheus"] == {
        "url": "http://prometheus.prometheus.svc",
        "port": 9090,
    }


def test_network_policy_admits_only_the_api_server_in_and_prometheus_out():
    spec = POLICY["spec"]
    assert spec["ingress"] == [
        {
            "fromEntities": ["kube-apiserver", "host", "remote-node"],
            "toPorts": [{"ports": [{"port": "6443", "protocol": "TCP"}]}],
        }
    ]
    targets = [
        rule.get("toEntities") or rule["toEndpoints"][0]["matchLabels"]
        for rule in spec["egress"]
    ]
    assert targets == [
        ["kube-apiserver"],
        {
            "k8s:io.kubernetes.pod.namespace": "prometheus",
            "app.kubernetes.io/name": "prometheus",
        },
    ]


def _series(**kw):
    labels = ",".join(f'{k}="{v}"' for k, v in {**POOL, **kw}.items())
    return labels


def _expect(series, want):
    """promtool prints the sample it got on a mismatch; assert on exp_samples."""
    matchers = ",".join(f'{k}="{v}"' for k, v in POOL.items())
    expr = RULE["metricsQuery"].replace("<<.LabelMatchers>>", matchers)
    exp = [] if want is None else [{"labels": "{" + _series() + "}", "value": want}]
    test = {
        "evaluation_interval": "1m",
        "tests": [
            {
                "interval": "1m",
                "input_series": [
                    {"series": name, "values": values}
                    for name, values in series.items()
                ],
                "promql_expr_test": [
                    {"expr": expr, "eval_time": "2m", "exp_samples": exp}
                ],
            }
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.yml"
        path.write_text(json.dumps(test))
        run = subprocess.run(
            [os.environ["PROMTOOL"], "test", "rules", str(path)],
            capture_output=True,
            text=True,
        )
    assert run.returncode == 0, run.stdout + run.stderr


def demand(source):
    return f"factory_worker_pool_demand{{{_series(source=source)}}}"


SPARE = f"factory_worker_pool_spare{{{_series()}}}"
OTHER = 'factory_worker_pool_demand{pool_namespace="factory-r-homelab-review",pool="homelab-reviewer",source="actors"}'


@pytest.mark.parametrize(
    ("series", "want"),
    [
        # Idle: both sources read 0, so the pool asks for nothing, spare or not.
        ({demand("actors"): "0 0 0", demand("pending"): "0 0 0", SPARE: "2 2 2"}, 0),
        # A run arrives at zero workers: pending wakes the pool with its spare.
        ({demand("actors"): "0 0 0", demand("pending"): "0 0 1", SPARE: "2 2 2"}, 3),
        # Live actors keep their workers, plus the spare.
        ({demand("actors"): "4 4 4", demand("pending"): "0 0 0", SPARE: "2 2 2"}, 6),
        # Both sources add.
        ({demand("actors"): "3 3 3", demand("pending"): "2 2 2", SPARE: "1 1 1"}, 6),
        # Another pool's series never count.
        (
            {
                demand("actors"): "1 1 1",
                demand("pending"): "0 0 0",
                SPARE: "0 0 0",
                OTHER: "9 9 9",
            },
            1,
        ),
        # The connector's source alone (the reaper's is missing) still wakes the pool.
        ({demand("pending"): "1 1 1", SPARE: "1 1 1"}, 2),
        # No spare series (the reaper stopped): no value, so the HPA holds.
        ({demand("actors"): "3 3 3", demand("pending"): "0 0 0"}, None),
    ],
    ids=[
        "idle",
        "wake",
        "live-actors",
        "sources-add",
        "other-pool",
        "pending-only",
        "no-spare",
    ],
)
def test_query_adds_demand_and_spare_while_the_pool_has_demand(series, want):
    _expect(series, want)
