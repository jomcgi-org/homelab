import os
from pathlib import Path
import subprocess

import yaml


def render(*values):
    args = []
    for value in ("image.digest=sha256:" + "a" * 64, *values):
        args += ["--set", value]
    result = subprocess.check_output(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "hikes",
            str(Path(__file__).parent),
            "--namespace",
            "hikes",
            *args,
        ],
        text=True,
    )
    return result, [doc for doc in yaml.safe_load_all(result) if doc]


def pod_of(doc):
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]


def assert_no_r2(result, documents):
    """No R2 credential, R2 endpoint or R2 egress renders in namespace hikes."""
    for marker in (
        "R2_BUCKET",
        "factory-r2-public-mirror",
        "r2-s3",
        "r2.cloudflarestorage.com",
    ):
        assert marker not in result
    assert {
        d["metadata"]["name"]: d["spec"]["itemPath"]
        for d in documents
        if d["kind"] == "OnePasswordItem"
    } == {"ghcr-imagepull-secret": "vaults/k8s-homelab/items/ghcr-read-permissions"}
    assert [d["metadata"]["name"] for d in documents if d["kind"] == "Secret"] == [
        "hikes-hikes-s3"
    ]
    for policy in (d for d in documents if d["kind"] == "CiliumNetworkPolicy"):
        for rule in policy["spec"].get("egress", []):
            assert "toCIDR" not in rule and "toEntities" not in rule


def test_writer_schedules_and_credentials():
    result, documents = render()
    assert sorted(
        d["kind"]
        for d in documents
        if d
        and d["metadata"].get("namespace") == "hikes"
        and d["kind"] in {"ResourceQuota", "LimitRange"}
    ) == ["LimitRange", "ResourceQuota"]
    claims = {
        doc["data"]["secretName"]: (doc["data"]["bucket"], doc["data"]["access"])
        for doc in documents
        if doc["kind"] == "ConfigMap"
        and doc["metadata"].get("labels", {}).get("factory.jomcgi.dev/garage-claim")
        == "true"
    }
    # The factory-public release mirrors the bucket (HOME-492). This
    # namespace holds no R2 credential and runs no mirror.
    assert claims == {"hikes-hikes-s3": ("hikes", "readwrite")}
    assert_no_r2(result, documents)
    crons = {d["metadata"]["name"]: d for d in documents if d["kind"] == "CronJob"}
    # HOME-477 turned the writers on. They sync in wave 1, after the bootstrap.
    assert {name: d["spec"]["suspend"] for name, d in crons.items()} == {
        "hikes-catalogue": False,
        "hikes-refresh": False,
        "hikes-prune": False,
    }
    assert all(
        d["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "1"
        for d in crons.values()
    )
    # met.no budget: odd hours at minute 40, clear of the hub's even-hour
    # refresh and both three-hourly Stars refreshes.
    assert crons["hikes-refresh"]["spec"]["schedule"] == "40 1-23/2 * * *"
    assert crons["hikes-prune"]["spec"]["schedule"] == "10 * * * *"
    assert crons["hikes-catalogue"]["spec"]["schedule"] == "45 5 1 * *"
    for mode in ("catalogue", "refresh", "prune"):
        container = pod_of(crons[f"hikes-{mode}"])["containers"][0]
        assert any(
            m["mountPath"] == "/var/lock/hikes" for m in container["volumeMounts"]
        )
        assert container["args"] == [mode]
        assert {e["valueFrom"]["secretKeyRef"]["name"] for e in container["env"]} == {
            "hikes-hikes-s3"
        }


def test_bootstrap_seeds_before_the_writers():
    _, documents = render()
    jobs = [d for d in documents if d["kind"] == "Job"]
    assert [d["metadata"]["name"] for d in jobs] == ["hikes-bootstrap"]
    job = jobs[0]
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "Sync"
    # The lock claim and the network policy sync in wave 0 with the hook.
    assert job["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "0"
    pod = pod_of(job)
    container = pod["containers"][0]
    assert container["args"] == ["bootstrap"]
    assert {
        "name": "writer-lock",
        "mountPath": "/var/lock/hikes",
        "readOnly": True,
    } in (container["volumeMounts"])
    assert {
        "name": "writer-lock",
        "persistentVolumeClaim": {"claimName": "hikes-writer-lock"},
    } in pod["volumes"]
    # The seed is the public jomcgi.dev copy of the hub snapshot, so the
    # hook holds only the writer's Garage credential.
    env = {e["name"]: e for e in container["env"]}
    assert env["SEED_URL"]["value"] == "https://jomcgi.dev/data/hikes/latest.json"
    assert {
        e["valueFrom"]["secretKeyRef"]["name"]
        for e in container["env"]
        if "valueFrom" in e
    } == {"hikes-hikes-s3"}
    policy = next(
        d
        for d in documents
        if d["kind"] == "CiliumNetworkPolicy"
        and d["metadata"]["name"] == "hikes-bootstrap"
    )
    fqdns = {
        f["matchName"]: [p["port"] for to in rule["toPorts"] for p in to["ports"]]
        for rule in policy["spec"]["egress"]
        for f in rule.get("toFQDNs", [])
    }
    assert fqdns == {"www.walkhighlands.co.uk": ["443"], "jomcgi.dev": ["443"]}


def test_suspension_removes_the_bootstrap():
    result, documents = render("suspendWriters=true")
    crons = [d for d in documents if d["kind"] == "CronJob"]
    assert len(crons) == 3 and all(d["spec"]["suspend"] for d in crons)
    assert not [d for d in documents if d["kind"] == "Job"]
    assert "SEED_URL" not in result
    assert_no_r2(result, documents)


def test_seed_url_must_be_https():
    try:
        render("seed.url=http://jomcgi.dev/data/hikes/latest.json")
    except subprocess.CalledProcessError:
        return
    raise AssertionError("an http seed URL rendered")
