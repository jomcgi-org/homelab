"""The chart owns the public bucket and mirror, and the health publisher."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

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
    # The sources own these prefixes; the root sync keeps them.
    assert "  --exclude '/stars-forecast/**' \\\n" in script
    assert "  --exclude '/hikes/**' \\\n" in script
    assert "  --exclude '/ships/**' \\\n" in script
    assert (
        'copyto "SRC0:factory-health/health.json" "R2:$R2_BUCKET/health.json" \\\n'
        '    --ignore-times --header-upload "Cache-Control: public, max-age=60"'
    ) in script


def test_sources_mirror_writer_buckets_under_their_prefixes():
    docs = render()
    for prefix in ("stars-forecast", "hikes", "ships"):
        name = f"factory-public-{prefix}-mirror"
        job = docs["CronJob", name]["spec"]
        # HOME-478 (Stars), HOME-477 (Hikes) and HOME-516 (Ships) unsuspend them.
        assert job["suspend"] is True
        assert job["schedule"] == "* * * * *"
        container = job["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert (env["GARAGE_BUCKET"], env["R2_BUCKET"]) == (prefix, "factory-public")
        script = container["command"][2]
        assert (
            f'run sync "GARAGE:$GARAGE_BUCKET" "R2:$R2_BUCKET/{prefix}" \\\n' in script
        )
        assert f'"R2:$R2_BUCKET/{prefix}/latest.json"' in script
        # Every R2 path in the script stays under the prefix.
        assert script.count('"R2:$R2_BUCKET') == script.count(
            f'"R2:$R2_BUCKET/{prefix}'
        )
        item = docs["OnePasswordItem", f"{name}-r2"]
        assert item["spec"]["itemPath"] == (
            "vaults/k8s-homelab/items/factory-r2-public-mirror"
        )


# A stand-in for rclone that keeps Garage and R2 as directories. Each object
# appears only when its copy completes, as on S3. With FAKE_INTERRUPT_AFTER=N,
# sync stops with an error after N transfers, before it deletes anything.
FAKE_RCLONE = r"""
import json, os, shutil, sys

args = sys.argv[1:]
assert args[:3] == ["--config", "/dev/null", "--s3-no-check-bucket"], args
command, rest = args[3], args[4:]
roots = {"GARAGE": os.environ["FAKE_GARAGE"], "R2": os.environ["FAKE_R2"]}


def local(path):
    remote, sep, name = path.partition(":")
    return os.path.join(roots[remote], name) if sep and remote in roots else path


def flag(name):
    return [rest[i + 1] for i, arg in enumerate(rest) if arg == name]


def files(root):
    out = set()
    for base, _, names in os.walk(root):
        for name in names:
            if not name.startswith("."):
                out.add(os.path.relpath(os.path.join(base, name), root))
    return out


def copy(source, target, headers):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(source, target + ".part")
    os.replace(target + ".part", target)
    log = os.path.join(os.environ["FAKE_R2"], ".headers.json")
    seen = json.load(open(log)) if os.path.exists(log) else {}
    seen[os.path.relpath(target, os.environ["FAKE_R2"])] = headers
    json.dump(seen, open(log, "w"))


if command == "lsf":
    root = local(rest[0])
    for pattern in flag("--include"):
        if os.path.isfile(os.path.join(root, pattern.lstrip("/"))):
            print(pattern.lstrip("/"))
elif command == "cat":
    sys.stdout.buffer.write(open(local(rest[0]), "rb").read())
elif command == "sync":
    source, target = local(rest[0]), local(rest[1])
    excluded = {e.lstrip("/") for e in flag("--exclude")}
    wanted = sorted(files(source) - excluded)
    budget = int(os.environ.get("FAKE_INTERRUPT_AFTER", "-1"))
    for name in wanted:
        dest = os.path.join(target, name)
        if os.path.exists(dest) and open(dest, "rb").read() == open(os.path.join(source, name), "rb").read():
            continue
        if budget == 0:
            sys.exit("interrupted")
        budget -= 1
        copy(os.path.join(source, name), dest, flag("--header-upload"))
    for name in files(target) - set(wanted) - excluded if os.path.isdir(target) else []:
        os.remove(os.path.join(target, name))
elif command == "copyto":
    copy(local(rest[0]), local(rest[1]), flag("--header-upload"))
elif command == "delete":
    for pattern in flag("--include"):
        path = os.path.join(local(rest[0]), pattern.lstrip("/"))
        if os.path.exists(path):
            os.remove(path)
else:
    sys.exit("unexpected command " + command)
"""


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
    """Read the generation that a public reader of the prefix sees."""
    manifest = json.loads((prefix / "latest.json").read_text())
    keys = {manifest["snapshot"], manifest["heat"]}
    snapshot = json.loads((prefix / manifest["snapshot"]).read_bytes())
    keys |= {v["track"] for v in snapshot["vessels"]}
    for key in keys:
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
    binary = tmp_path / "bin" / "rclone"
    binary.parent.mkdir()
    binary.write_text("#!" + shutil.which("python3") + "\n" + FAKE_RCLONE)
    binary.chmod(0o755)
    env = {
        **os.environ,
        **{var["name"]: var["value"] for var in container["env"] if "value" in var},
        "FAKE_GARAGE": str(garage),
        "FAKE_R2": str(r2),
        "PATH": str(binary.parent) + os.pathsep + os.environ["PATH"],
    }
    env["RCLONE_CONFIG_R2_ENDPOINT"] = "https://" + env["R2_HOST"]

    def mirror(interrupt_after=-1):
        run_env = dict(env, FAKE_INTERRUPT_AFTER=str(interrupt_after))
        return subprocess.run(container["command"], env=run_env, capture_output=True)

    first = generation(bucket, 48.5)
    assert mirror().returncode == 0
    assert resolve(prefix) == first

    # The writer publishes a second generation and keeps the first one's
    # objects. The mirror stops after one transfer.
    second = generation(bucket, 48.6)
    result = mirror(interrupt_after=1)
    assert result.returncode != 0
    # Readers still get the first generation, every object complete.
    assert resolve(prefix) == first

    # A long outage: the writer's grace period ends and it removes the
    # objects that only the first generation names.
    for key in first - second:
        (bucket / key).unlink()
    assert mirror().returncode == 0
    assert resolve(prefix) == second
    stored = {p.relative_to(prefix).as_posix() for p in prefix.rglob("*.json")}
    assert stored == second | {"latest.json"}
    headers = json.loads((r2 / ".headers.json").read_text())
    assert headers["factory-public/ships/latest.json"] == [
        "Cache-Control: public, max-age=60"
    ]
    for key in second:
        assert headers[f"factory-public/ships/{key}"] == [
            "Cache-Control: public, max-age=31536000, immutable"
        ]
