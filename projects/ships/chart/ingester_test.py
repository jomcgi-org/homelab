import os
from pathlib import Path
import subprocess

import yaml


def test_shadow_activation():
    # The chart default runs zero replicas until HOME-483 sets enabled=true.
    for overrides, replicas in (([], 0), (["--set", "enabled=true"], 1)):
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
        spec = next(
            doc["spec"]
            for doc in yaml.safe_load_all(result)
            if doc and doc["kind"] == "Deployment"
        )
        assert spec["replicas"] == replicas
        assert spec["strategy"]["type"] == "Recreate"
        container = spec["template"]["spec"]["containers"][0]
        assert container["env"][-1]["value"] == "false"


def test_sized_database():
    rendered = subprocess.check_output(
        [
            os.getenv("HELM_BIN", "helm"),
            "template",
            "ships",
            str(Path(__file__).parent),
            "--set",
            "image.digest=sha256:" + "0" * 64,
            "--set",
            "database.storage=5Gi,database.resources.requests.memory=128Mi",
        ]
    )
    docs = [d for d in yaml.safe_load_all(rendered) if d]
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


def render_publisher(*overrides):
    result = subprocess.check_output(
        [
            os.getenv("HELM_BIN", "helm"),
            "template",
            "ships",
            str(Path(__file__).parent),
            "--namespace",
            "ships",
            "--set",
            "image.digest=sha256:" + "0" * 64,
            "--set",
            "database.storage=5Gi,database.resources.requests.memory=128Mi",
            *overrides,
        ],
        text=True,
    )
    docs = [d for d in yaml.safe_load_all(result) if d]
    return result, {(d["kind"], d["metadata"]["name"]): d for d in docs}


def test_publisher_stays_suspended_until_activation():
    # HOME-516 sets publisher.suspend=false when it turns the ingester on.
    for overrides, suspended in (
        ([], True),
        (["--set", "publisher.suspend=false"], False),
    ):
        _, docs = render_publisher(*overrides)
        job = docs["CronJob", "ships-publish"]["spec"]
        assert job["suspend"] is suspended
        assert job["schedule"] == "*/2 * * * *"
        assert job["concurrencyPolicy"] == "Forbid"


def test_publisher_writes_only_its_garage_bucket():
    rendered, docs = render_publisher()
    claims = {
        (d["data"]["secretName"], d["data"]["bucket"], d["data"]["access"])
        for (kind, _), d in docs.items()
        if kind == "ConfigMap" and d["metadata"].get("namespace") == "garage"
    }
    # The factory-public release mirrors the bucket to R2 under ships/
    # (HOME-492). This namespace holds no R2 credential and runs no mirror.
    assert claims == {("ships-ships-s3", "ships", "readwrite")}
    assert "R2_BUCKET" not in rendered
    assert "factory-r2-public-mirror" not in rendered
    pod = docs["CronJob", "ships-publish"]["spec"]["jobTemplate"]["spec"]["template"][
        "spec"
    ]
    container = pod["containers"][0]
    assert container["args"] == ["publish"]
    assert container["image"].endswith("@sha256:" + "0" * 64)
    assert {e["valueFrom"]["secretKeyRef"]["name"] for e in container["env"]} == {
        "ships-ships-s3",
        "ships-db-app",
    }
    assert pod["automountServiceAccountToken"] is False
    # Egress: Garage from the library policy, the database from the chart.
    library = docs["CiliumNetworkPolicy", "ships-publish"]["spec"]
    assert library["enableDefaultDeny"] == {"ingress": True, "egress": True}
    assert [rule["toPorts"][0]["ports"][0]["port"] for rule in library["egress"]] == [
        "3900"
    ]
    database = docs["CiliumNetworkPolicy", "ships-publish-db"]["spec"]
    assert database["endpointSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "ships-publish"
    }
    assert database["egress"][0]["toEndpoints"] == [
        {"matchLabels": {"cnpg.io/cluster": "ships-db"}}
    ]
    ingress = docs["CiliumNetworkPolicy", "ships-db"]["spec"]["ingress"][0]
    assert {"app.kubernetes.io/name": "ships-publish"} in [
        e["matchLabels"] for e in ingress["fromEndpoints"]
    ]
