"""The first consumer needs only one bucket entry, including its public mirror."""

import os
from pathlib import Path
import subprocess

import yaml

HERE = Path(__file__).resolve().parent


def test_public_consumer():
    config = yaml.safe_load((HERE / "values.yaml").read_text())
    assert list(config) == ["buckets"]
    assert len(config["buckets"]) == 1
    result = subprocess.run(
        [
            os.environ["HELM_BIN"],
            "template",
            "factory-public",
            str(HERE),
            "--namespace",
            "factory-public",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    job = next(d for d in docs if d["kind"] == "CronJob")
    pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == job["metadata"]["name"]
    assert job["spec"]["schedule"] == "* * * * *"
    assert job["spec"]["concurrencyPolicy"] == "Forbid"
    assert job["spec"]["suspend"] is True
    item = next(d for d in docs if d["kind"] == "OnePasswordItem")
    assert item["spec"]["itemPath"] == (
        "vaults/k8s-homelab/items/factory-r2-public-mirror"
    )
    # garage-init grants keys only from claims in the protected garage namespace.
    claims = [d for d in docs if d["kind"] == "ConfigMap"]
    assert {c["metadata"]["namespace"] for c in claims} == {"garage"}
    assert {
        (
            c["data"]["namespace"],
            c["data"]["secretName"],
            c["data"]["bucket"],
            c["data"]["access"],
        )
        for c in claims
    } == {
        (
            "factory-public",
            "factory-public-factory-public-s3",
            "factory-public",
            "read",
        ),
        (
            "factory-public",
            "factory-public-factory-public-mirror-s3",
            "factory-public",
            "read",
        ),
    }
    # Only the mirror's ServiceAccount may mount the R2 Secret: one denying
    # policy each for Pods, Jobs and CronJobs, each with its binding.
    policies = [d for d in docs if d["kind"] == "ValidatingAdmissionPolicy"]
    bindings = {
        d["spec"]["policyName"]: d["spec"]["validationActions"]
        for d in docs
        if d["kind"] == "ValidatingAdmissionPolicyBinding"
    }
    assert set(bindings) == {p["metadata"]["name"] for p in policies}
    assert all(actions == ["Deny"] for actions in bindings.values())
    assert sorted(
        resource
        for p in policies
        for rule in p["spec"]["matchConstraints"]["resourceRules"]
        for resource in rule["resources"]
    ) == ["cronjobs", "jobs", "pods", "pods/ephemeralcontainers"]
    assert all(
        '"factory-public-factory-public-mirror"' in v["expression"]
        for p in policies
        for v in p["spec"]["validations"]
    )
    policy = next(d for d in docs if d["kind"] == "CiliumNetworkPolicy")["spec"]
    assert policy["enableDefaultDeny"] == {"ingress": True, "egress": True}
    assert [rule["toPorts"][0]["ports"][0]["port"] for rule in policy["egress"]] == [
        "3900",
        "443",
        "53",
    ]
    assert policy["egress"][1]["toFQDNs"] == [
        {"matchName": "7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com"}
    ]
    application = yaml.safe_load(
        Path(
            os.environ.get(
                "PUBLIC_APPLICATION", str(HERE.parent / "deploy/application.yaml")
            )
        ).read_text()
    )
    spec = application["spec"]
    assert "RespectIgnoreDifferences=true" in spec["syncPolicy"]["syncOptions"]
    assert "ServerSideApply=true" in spec["syncPolicy"]["syncOptions"]
    assert {entry["name"] for entry in spec["ignoreDifferences"]} == {
        d["metadata"]["name"] for d in docs if d["kind"] == "Secret"
    }
    assert all(
        entry["jsonPointers"] == ["/data"] for entry in spec["ignoreDifferences"]
    )
