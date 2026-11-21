import os
from pathlib import Path
import subprocess

import yaml


def render(*overrides):
    result = subprocess.check_output(
        [
            os.getenv("HELM_BIN", "helm"),
            "template",
            "ships",
            str(Path(__file__).parent),
            "--set",
            "image.digest=sha256:" + "0" * 64,
            *overrides,
        ]
    )
    return list(yaml.safe_load_all(result))


def deployment(docs):
    return next(d["spec"] for d in docs if d["kind"] == "Deployment")


def test_activation():
    # HOME-537 runs one replica by default. enabled=false is the rollback.
    for overrides, replicas in (([], 1), (["--set", "enabled=false"], 0)):
        spec = deployment(render(*overrides))
        assert spec["replicas"] == replicas
        assert spec["strategy"]["type"] == "Recreate"
        container = spec["template"]["spec"]["containers"][0]
        assert container["env"][-1]["value"] == "false"


def test_cutover_defaults():
    # The starting budgets that ARCHITECTURE.md states, at one replica.
    docs = render()
    spec = deployment(docs)
    assert spec["replicas"] == 1
    container = spec["template"]["spec"]["containers"][0]
    assert container["resources"] == {
        "requests": {"cpu": "250m", "memory": "512Mi"},
        "limits": {"cpu": "1", "memory": "1Gi"},
    }
    cluster = next(d["spec"] for d in docs if d["kind"] == "Cluster")
    assert cluster["storage"] == {"storageClass": "local-path", "size": "20Gi"}
    assert cluster["resources"] == {
        "requests": {"cpu": "100m", "memory": "512Mi"},
        "limits": {"memory": "1Gi"},
    }
    # The hub reads the same 1Password item and key (monolith marine values).
    key = next(e for e in container["env"] if e["name"] == "AISSTREAM_API_KEY")
    ref = key["valueFrom"]["secretKeyRef"]
    assert ref["key"] == "ais-stream-api-key"
    item = next(
        d
        for d in docs
        if d["kind"] == "OnePasswordItem" and d["metadata"]["name"] == ref["name"]
    )
    assert item["spec"]["itemPath"] == "vaults/k8s-homelab/items/marine"
    policy = next(
        d["spec"]
        for d in docs
        if d["kind"] == "CiliumNetworkPolicy"
        and d["metadata"]["name"] == "ships-ingester"
    )
    assert {
        "toFQDNs": [{"matchName": "stream.aisstream.io"}],
        "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
    } in policy["egress"]


def test_sized_database():
    docs = render(
        "--set", "database.storage=5Gi,database.resources.requests.memory=128Mi"
    )
    cluster = next(d["spec"] for d in docs if d["kind"] == "Cluster")
    assert cluster["storage"]["size"] == "5Gi"
    (ref,) = cluster["bootstrap"]["initdb"]["postInitApplicationSQLRefs"][
        "configMapRefs"
    ]
    config = next(
        d
        for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == ref["name"]
    )
    assert "CREATE TABLE ships.heat_cells_historical" in config["data"][ref["key"]]


def test_namespace_policy():
    docs = render("--namespace", "ships")
    assert sorted(
        d["kind"]
        for d in docs
        if d["metadata"].get("namespace") == "ships"
        and d["kind"] in {"ResourceQuota", "LimitRange"}
    ) == ["LimitRange", "ResourceQuota"]
