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
        "factory-storyboard-readers",
        "factory-storyboard-db",
    }
    # The root filesystem stays read-only; the secret scan writes to /tmp.
    (container,) = pod["containers"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["volumeMounts"] == [{"name": "tmp", "mountPath": "/tmp"}]
    assert pod["volumes"] == [{"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}]
    # The 1Password item needs only the read-only itsaplan key.
    assert [
        ref["key"] for ref in refs if ref["name"] == "factory-storyboard-readers"
    ] == ["ITSAPLAN_KEY"]


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
