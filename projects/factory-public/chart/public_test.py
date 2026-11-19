"""The chart owns the public bucket and mirror, and the health publisher."""

import os
from pathlib import Path
import subprocess

import yaml

HERE = Path(__file__).resolve().parent


def test_public_consumer():
    config = yaml.safe_load((HERE / "values.yaml").read_text())
    assert list(config) == ["buckets", "workloads", "secrets"]
    assert [b["name"] for b in config["buckets"]] == [
        "factory-public",
        "factory-health",
    ]
    assert "public" not in config["buckets"][1]
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
    by_name = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    job = by_name["CronJob", "factory-public-factory-public-mirror"]
    pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == job["metadata"]["name"]
    assert job["spec"]["schedule"] == "* * * * *"
    assert job["spec"]["concurrencyPolicy"] == "Forbid"
    assert job["spec"]["suspend"] is False
    item = by_name["OnePasswordItem", "factory-public-factory-public-mirror-r2"]
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
        # The health publisher writes only its own bucket; the mirror reads it.
        (
            "factory-public",
            "factory-public-factory-health-s3",
            "factory-health",
            "write",
        ),
        (
            "factory-public",
            "factory-public-factory-public-mirror-factory-health-s3",
            "factory-health",
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
    policy = by_name["CiliumNetworkPolicy", "factory-public-factory-public-mirror"][
        "spec"
    ]
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


def render():
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
    return {
        (d["kind"], d["metadata"]["name"]): d
        for d in yaml.safe_load_all(result.stdout)
        if d
    }


def test_health_publisher_holds_no_factory_public_credential():
    docs = render()
    job = docs["CronJob", "health"]
    assert job["spec"]["schedule"] == "*/2 * * * *"
    pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "health"
    container = pod["containers"][0]
    assert container["command"] == ["python3", "-B", "-c"]
    assert container["args"] == [(HERE / "files" / "health.py").read_text()]
    refs = {
        e["valueFrom"]["secretKeyRef"]["name"]: e["valueFrom"]["secretKeyRef"]
        for e in container["env"]
    }
    # Its Garage key covers factory-health only, never factory-public.
    assert set(refs) == {
        "factory-public-factory-health-s3",
        "factory-health-cloudflare",
    }
    assert refs["factory-health-cloudflare"]["optional"] is True
    item = docs["OnePasswordItem", "factory-health-cloudflare"]
    assert item["spec"]["itemPath"] == (
        "vaults/k8s-homelab/items/factory-health-cloudflare"
    )
    egress = docs["CiliumNetworkPolicy", "health"]["spec"]["egress"]
    assert [rule["toPorts"][0]["ports"][0]["port"] for rule in egress] == [
        "6443",
        "443",
        "3900",
    ]
    assert egress[1]["toFQDNs"] == [{"matchName": "api.cloudflare.com"}]
    # Read-only API access: get one named Application, nothing cluster-wide.
    assert not [
        kind for kind, _ in docs if kind.startswith("Cluster") and "Role" in kind
    ]
    apps = docs["Role", "factory-public-health-apps"]
    assert apps["metadata"]["namespace"] == "argocd"
    assert apps["rules"] == [
        {
            "apiGroups": ["argoproj.io"],
            "resources": ["applications"],
            "resourceNames": ["argocd"],
            "verbs": ["get"],
        }
    ]


def test_mirror_publishes_only_health_json_from_the_health_bucket():
    job = render()["CronJob", "factory-public-factory-public-mirror"]
    script = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0][
        "command"
    ][2]
    assert "--exclude /health.json" in script
    assert (
        'copyto "SRC0:factory-health/health.json" "R2:$R2_BUCKET/health.json" \\\n'
        '    --ignore-times --header-upload "Cache-Control: public, max-age=60"'
    ) in script
