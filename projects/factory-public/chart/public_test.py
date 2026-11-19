"""The chart owns the public bucket and mirror, and the health publisher."""

import os
from pathlib import Path
import subprocess

import yaml

HERE = Path(__file__).resolve().parent
# The root mirror, then one mirror for each public.sources entry (HOME-492).
MIRRORS = [
    "factory-public-factory-public-mirror",
    "factory-public-stars-forecast-mirror",
    "factory-public-hikes-mirror",
]


def test_public_consumer():
    config = yaml.safe_load((HERE / "values.yaml").read_text())
    assert list(config) == ["buckets", "workloads", "secrets", "healthAlertTest"]
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
        # The source mirrors read the writers' buckets and write none (HOME-492).
        (
            "factory-public",
            "factory-public-stars-forecast-mirror-s3",
            "stars-forecast",
            "read",
        ),
        (
            "factory-public",
            "factory-public-hikes-mirror-s3",
            "hikes",
            "read",
        ),
    }
    # Only each mirror's ServiceAccount may mount its R2 Secret: one denying
    # policy each for Pods, Jobs and CronJobs, each with its binding.
    policies = [d for d in docs if d["kind"] == "ValidatingAdmissionPolicy"]
    bindings = {
        d["spec"]["policyName"]: d["spec"]["validationActions"]
        for d in docs
        if d["kind"] == "ValidatingAdmissionPolicyBinding"
    }
    assert set(bindings) == {p["metadata"]["name"] for p in policies}
    assert all(actions == ["Deny"] for actions in bindings.values())
    for mirror in MIRRORS:
        own = [
            p
            for p in policies
            if p["metadata"]["name"].startswith(f"factory-public-{mirror}-r2")
        ]
        assert sorted(
            resource
            for p in own
            for rule in p["spec"]["matchConstraints"]["resourceRules"]
            for resource in rule["resources"]
        ) == ["cronjobs", "jobs", "pods", "pods/ephemeralcontainers"]
        assert all(
            f'"{mirror}"' in p["spec"]["validations"][0]["expression"] for p in own
        )
    assert len(policies) == 3 * len(MIRRORS)
    # The library README's writer rule depends on this deadline.
    for mirror in MIRRORS:
        job = by_name["CronJob", mirror]["spec"]["jobTemplate"]["spec"]
        assert job["activeDeadlineSeconds"] > 0
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


def render(*args):
    result = subprocess.run(
        [
            os.environ["HELM_BIN"],
            "template",
            "factory-public",
            str(HERE),
            "--namespace",
            "factory-public",
            *args,
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
        if "valueFrom" in e
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


def test_alert_test_renders_each_setting():
    # The HOME-415 alert check (README "Alert check"). Each setting renders
    # its own value; deploy/application.yaml sets it for the check.
    for setting in ("true", "false"):
        job = render("--set", f"healthAlertTest={setting}")["CronJob", "health"]
        pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        env = pod["containers"][0]["env"]
        assert [e for e in env if e["name"] == "ALERT_TEST"] == [
            {"name": "ALERT_TEST", "value": setting}
        ]


def test_mirror_publishes_only_health_json_from_the_health_bucket():
    job = render()["CronJob", "factory-public-factory-public-mirror"]
    script = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0][
        "command"
    ][2]
    assert "--exclude /health.json" in script
    # The sources own these prefixes; the root sync keeps them.
    assert "  --exclude '/stars-forecast/**' \\\n" in script
    assert "  --exclude '/hikes/**' \\\n" in script
    health = (
        'copyto "SRC0:factory-health/health.json" "R2:$R2_BUCKET/health.json" \\\n'
        '    --ignore-times --header-upload "Cache-Control: public, max-age=60"'
    )
    assert health in script
    # Garage listings around the saved manifest, payloads without deletes,
    # the saved manifest and health.json, then the deletes.
    assert (
        script.index('cat "GARAGE:$GARAGE_BUCKET/manifest.json"')
        < script.index('\npayloads copy "GARAGE:$GARAGE_BUCKET" "R2:$R2_BUCKET"')
        < script.index('copyto "$scratch/manifest.json" "R2:$R2_BUCKET/manifest.json"')
        < script.index(health)
        < script.index('run delete "R2:$R2_BUCKET" --files-from-raw')
    )


def test_sources_mirror_writer_buckets_under_their_prefixes():
    docs = render()
    for prefix in ("stars-forecast", "hikes"):
        name = f"factory-public-{prefix}-mirror"
        job = docs["CronJob", name]["spec"]
        # HOME-478 (Stars) and HOME-477 (Hikes) turned them on.
        assert job["suspend"] is False
        assert job["schedule"] == "* * * * *"
        container = job["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert (env["GARAGE_BUCKET"], env["R2_BUCKET"]) == (prefix, "factory-public")
        script = container["command"][2]
        # Payloads without deletes, then the saved manifest, then the deletes.
        dest = f'"R2:$R2_BUCKET/{prefix}"'
        assert (
            script.index('cat "GARAGE:$GARAGE_BUCKET/latest.json"')
            < script.index(f'\npayloads copy "GARAGE:$GARAGE_BUCKET" {dest}')
            < script.index(
                f'copyto "$scratch/latest.json" "R2:$R2_BUCKET/{prefix}/latest.json"'
            )
            < script.index(f"run delete {dest} --files-from-raw")
        )
        # Every R2 path in the script stays under the prefix.
        assert script.count('"R2:$R2_BUCKET') == script.count(
            f'"R2:$R2_BUCKET/{prefix}'
        )
        item = docs["OnePasswordItem", f"{name}-r2"]
        assert item["spec"]["itemPath"] == (
            "vaults/k8s-homelab/items/factory-r2-public-mirror"
        )
