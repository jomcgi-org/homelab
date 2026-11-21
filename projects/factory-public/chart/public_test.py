"""The chart owns the public bucket and mirror, and the health publisher."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import yaml

HERE = Path(__file__).resolve().parent
# The root mirror, then one mirror for each public.sources entry (HOME-492).
MIRRORS = [
    "factory-public-factory-public-mirror",
    "factory-public-stars-forecast-mirror",
    "factory-public-hikes-mirror",
    "factory-public-ships-mirror",
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
        (
            "factory-public",
            "factory-public-ships-mirror-s3",
            "ships",
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


def secret_refs(node):
    """Return the Secret names of every secretKeyRef and secretRef in node."""
    if isinstance(node, list):
        return set().union(*(secret_refs(item) for item in node))
    if not isinstance(node, dict):
        return set()
    names = {
        node[key]["name"]
        for key in ("secretKeyRef", "secretRef")
        if isinstance(node.get(key), dict)
    }
    return names.union(*(secret_refs(value) for value in node.values()))


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
    assert "  --exclude '/ships/**' \\\n" in script
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
    for prefix in ("stars-forecast", "hikes", "ships"):
        name = f"factory-public-{prefix}-mirror"
        job = docs["CronJob", name]["spec"]
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


def test_cloudflare_exporter_checks_the_health_token_only():
    # HOME-634: the health token, checked in its own namespace. Only the
    # mirror CronJobs hold the R2 mirror key, so the exporter reads no R2 key
    # and no egress IP. The script is the prometheus copy.
    docs = render()
    script = Path(
        os.environ.get(
            "CLOUDFLARE_EXPORTER",
            str(
                HERE.parents[2]
                / "platform-factory/prometheus/resources/cloudflare_exporter.py"
            ),
        )
    ).read_text()
    assert (HERE / "files" / "cloudflare_exporter.py").read_text() == script
    pod = docs["Deployment", "cloudflare-exporter"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "cloudflare-exporter"
    assert pod["automountServiceAccountToken"] is False
    assert "volumes" not in pod
    [container] = pod["containers"]
    assert container["command"] == ["python3", "-B", "-c"]
    assert container["args"] == [script]
    assert "envFrom" not in container
    env = {e["name"]: e for e in container["env"]}
    # The egress IP never enters the public data namespace.
    assert "TRACE_URL" not in env
    assert not [name for name in env if name.startswith("R2_")]
    refs = sorted(
        (
            e["valueFrom"]["secretKeyRef"]["name"],
            e["valueFrom"]["secretKeyRef"]["key"],
            e["valueFrom"]["secretKeyRef"].get("optional", False),
        )
        for e in env.values()
        if "valueFrom" in e
    )
    assert refs == [("factory-health-cloudflare", "api-token", True)]
    assert env["API_URL"]["value"] == (
        "https://api.cloudflare.com/client/v4/accounts/"
        "7c56b458cd657d96b095c63d181c051f/workers/scripts/jomcgi-site/deployments"
    )
    # Only the mirror CronJobs hold the R2 mirror key.
    r2_secrets = {
        name
        for (kind, name), doc in docs.items()
        if kind == "OnePasswordItem"
        and doc["spec"]["itemPath"]
        == "vaults/k8s-homelab/items/factory-r2-public-mirror"
    }
    assert r2_secrets == {f"{mirror}-r2" for mirror in MIRRORS}
    holders = {key for key, doc in docs.items() if secret_refs(doc) & r2_secrets}
    assert holders == {("CronJob", mirror) for mirror in MIRRORS}
    policy = docs["CiliumNetworkPolicy", "cloudflare-exporter"]["spec"]
    assert policy["enableDefaultDeny"] == {"ingress": True, "egress": True}
    assert "ingress" not in policy
    assert policy["egress"] == [
        {
            "toFQDNs": [{"matchName": "api.cloudflare.com"}],
            "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
        }
    ]


def payload(root, name, value):
    """Write a payload the way the Ships publisher does: its digest in its key."""
    body = json.dumps(value).encode()
    key = f"{name}-{hashlib.sha256(body).hexdigest()}.json"
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return key


def generation(root, lat):
    track = payload(root, "tracks/111", {"mmsi": "111", "track": [{"lat": lat}]})
    heat = payload(root, "heat", {"cells": [[1, 2, 3]]})
    snapshot = payload(root, "snapshot", {"vessels": [{"mmsi": "111", "track": track}]})
    (root / "latest.json").write_text(json.dumps({"snapshot": snapshot, "heat": heat}))
    return {snapshot, heat, track}


def resolve(prefix):
    """Read the generation that a public reader of the prefix sees.

    Every key it names must exist with the bytes that its digest names.
    """
    manifest = json.loads((prefix / "latest.json").read_text())
    keys = {manifest["snapshot"], manifest["heat"]}
    assert (prefix / manifest["snapshot"]).exists(), (
        "latest.json names a missing snapshot"
    )
    snapshot = json.loads((prefix / manifest["snapshot"]).read_bytes())
    keys |= {v["track"] for v in snapshot["vessels"]}
    for key in keys:
        assert (prefix / key).exists(), f"latest.json names missing {key}"
        digest = key.rsplit("-", 1)[1].removesuffix(".json")
        assert hashlib.sha256((prefix / key).read_bytes()).hexdigest() == digest, key
    return keys


def test_ships_mirror_survives_an_interrupted_copy(tmp_path):
    job = render()["CronJob", "factory-public-ships-mirror"]
    container = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    garage, r2 = tmp_path / "garage", tmp_path / "r2"
    bucket, prefix = garage / "ships", r2 / "factory-public" / "ships"
    bucket.mkdir(parents=True)
    prefix.mkdir(parents=True)
    # The library's directory-backed rclone stand-in (fake_rclone.py).
    binary = tmp_path / "bin" / "rclone"
    binary.parent.mkdir()
    binary.write_text(
        "#!"
        + shutil.which("python3")
        + "\n"
        + Path(os.environ["FAKE_RCLONE"]).read_text()
    )
    binary.chmod(0o755)

    def mirror(r2_root, budget=None):
        state = tempfile.mkdtemp(dir=tmp_path)
        env = {
            **os.environ,
            **{var["name"]: var["value"] for var in container["env"] if "value" in var},
            "FAKE_REMOTES": json.dumps({"GARAGE": str(garage), "R2": str(r2_root)}),
            "FAKE_STATE": state,
            "PATH": str(binary.parent) + os.pathsep + os.environ["PATH"],
        }
        env["RCLONE_CONFIG_R2_ENDPOINT"] = "https://" + env["R2_HOST"]
        if budget is not None:
            env["FAKE_BUDGET"] = str(budget)
        result = subprocess.run(container["command"], env=env, capture_output=True)
        headers = Path(state) / "headers.json"
        return result, json.loads(headers.read_text()) if headers.exists() else {}

    def interrupt_at_every_step(expected):
        """Stop a run before each call and between each object write or delete.

        Each run starts from the same R2 state. Readers always resolve a
        complete generation: the one before the run, or the new one.
        """
        budget = 0
        while True:
            attempt = Path(tempfile.mkdtemp(dir=tmp_path)) / "r2"
            shutil.copytree(r2, attempt)
            result, _ = mirror(attempt, budget)
            assert resolve(attempt / "factory-public" / "ships") in expected, budget
            if result.returncode == 0:
                return budget
            assert b"interrupted" in result.stderr, result.stderr
            budget += 1

    first = generation(bucket, 48.5)
    assert mirror(r2)[0].returncode == 0
    assert resolve(prefix) == first

    # The writer publishes a second generation and keeps the first one's
    # objects. The mirror stops after one transfer.
    second = generation(bucket, 48.6)
    assert mirror(r2, budget=6)[0].returncode != 0
    # Readers still get the first generation, every object complete.
    assert resolve(prefix) == first
    assert interrupt_at_every_step([first, second]) > 0

    # A long outage: the writer's grace period ends and it removes the
    # objects that only the first generation names. R2 still serves them
    # under its first latest.json, so the mirror deletes them only after it
    # replaces that manifest.
    for key in first - second:
        (bucket / key).unlink()
    # Two Garage listings around the manifest lookup and cat, the copy and
    # its upload of the second track, the R2 listing, copyto and its upload,
    # then the delete and its two deletes: 12 steps.
    assert interrupt_at_every_step([first, second]) == 12

    # Recovery: the next complete run catches up.
    result, headers = mirror(r2)
    assert result.returncode == 0
    assert resolve(prefix) == second
    stored = {p.relative_to(prefix).as_posix() for p in prefix.rglob("*.json")}
    assert stored == second | {"latest.json"}
    assert headers[str(prefix / "latest.json")] == ["Cache-Control: public, max-age=60"]
    _, headers = mirror(tmp_path / "fresh")
    for key in second:
        assert headers[str(tmp_path / "fresh" / "factory-public" / "ships" / key)] == [
            "Cache-Control: public, max-age=31536000, immutable"
        ]
