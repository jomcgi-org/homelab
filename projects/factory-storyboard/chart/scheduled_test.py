import os
from pathlib import Path
import subprocess

import yaml

WORKLOADS = {"Pod", "Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}


def render():
    manifests = subprocess.check_output(
        [
            os.getenv("HELM_BIN", "helm"),
            "template",
            "factory-storyboard",
            str(Path(__file__).parent),
            "-n",
            "factory",
            "--set",
            "image.digest=sha256:" + "0" * 64,
        ]
    )
    return [d for d in yaml.safe_load_all(manifests) if d]


def test_scheduled_writer():
    docs = render()
    jobs = [d for d in docs if d["kind"] == "CronJob"]
    assert len(jobs) == 1
    job = jobs[0]["spec"]
    assert job["schedule"] == "*/5 * * * *"
    assert job["concurrencyPolicy"] == "Forbid"
    pod = job["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "factory-storyboard"
    assert pod["automountServiceAccountToken"] is False
    assert any(
        d["kind"] == "ServiceAccount"
        and d["metadata"]["name"] == pod["serviceAccountName"]
        for d in docs
    )
    refs = [
        v["valueFrom"]["secretKeyRef"]
        for v in pod["containers"][0]["env"]
        if "valueFrom" in v
    ]
    assert {ref["name"] for ref in refs} == {
        "factory-storyboard-factory-public-s3",
        "factory-itsaplan-agent-factory-storyboard",
        "factory-storyboard-db",
    }
    (claim,) = [
        d["data"]
        for d in docs
        if d["kind"] == "ConfigMap"
        and d["metadata"].get("labels", {}).get("factory.jomcgi.dev/garage-claim")
        == "true"
    ]
    assert claim == {
        "access": "readwrite",
        "bucket": "factory-public",
        "namespace": "factory",
        "secretName": "factory-storyboard-factory-public-s3",
    }
    # The root filesystem stays read-only; the secret scan writes to /tmp.
    (container,) = pod["containers"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["volumeMounts"] == [{"name": "tmp", "mountPath": "/tmp"}]
    assert pod["volumes"] == [{"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}]


def test_itsaplan_key_comes_from_the_bootstrap_secret():
    # The factory chart's itsaplan bootstrap creates the read-only agent
    # factory-storyboard and writes its key to this Secret under apiKey
    # (projects/factory/chart/files/itsaplan/bootstrap.py).
    docs = render()
    (job,) = [d for d in docs if d["kind"] == "CronJob"]
    pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    env = {v["name"]: v for v in pod["containers"][0]["env"]}
    # The CronJob can run before that PostSync bootstrap. An optional
    # reference lets the pod start, and the publisher then skips the cycle.
    assert env["ITSAPLAN_KEY"]["valueFrom"] == {
        "secretKeyRef": {
            "name": "factory-itsaplan-agent-factory-storyboard",
            "key": "apiKey",
            "optional": True,
        }
    }
    # No other Secret reference is optional.
    assert [
        name
        for name, var in env.items()
        if var.get("valueFrom", {}).get("secretKeyRef", {}).get("optional")
    ] == ["ITSAPLAN_KEY"]
    assert "APPROVED_HISTORY" not in env
    # The chart owns no copy of that Secret and no 1Password item.
    kinds = {(d["kind"], d["metadata"]["name"]) for d in docs}
    assert not any(kind == "OnePasswordItem" for kind, _ in kinds)
    assert not any(name.startswith("factory-itsaplan") for _, name in kinds)


def test_no_pod_reaches_the_kagent_controller():
    # kagent's trusted-proxy listener (:8083) trusts unverified bearer JWTs
    # (projects/factory/ARCHITECTURE.md finding 5). Task history comes from
    # the connector's journal in factory-db, so the chart runs one pod whose
    # default-deny egress allows only factory-db, itsaplan and Garage.
    docs = render()
    workloads = [(d["kind"], d["metadata"]["name"]) for d in docs]
    assert [w for w in workloads if w[0] in WORKLOADS] == [
        ("CronJob", "factory-storyboard")
    ]
    assert not [w for w in workloads if w[0] == "Service"]
    (policy,) = [d["spec"] for d in docs if d["kind"] == "CiliumNetworkPolicy"]
    assert policy["endpointSelector"] == {
        "matchLabels": {"app.kubernetes.io/name": "factory-storyboard"}
    }
    assert policy["enableDefaultDeny"] == {"egress": True, "ingress": True}
    assert "ingress" not in policy
    peers = set()
    for rule in policy["egress"]:
        assert set(rule) == {"toEndpoints", "toPorts"}
        ports = [p["port"] for r in rule["toPorts"] for p in r["ports"]]
        for endpoint in rule["toEndpoints"]:
            labels = endpoint["matchLabels"]
            peers.update(
                (labels["k8s:io.kubernetes.pod.namespace"], port) for port in ports
            )
    assert peers == {("factory-db", "5432"), ("factory", "3000"), ("garage", "3900")}
