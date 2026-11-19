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


def test_writer_schedules_and_credentials():
    result, documents = render()
    claims = {
        doc["data"]["secretName"]: (doc["data"]["bucket"], doc["data"]["access"])
        for doc in documents
        if doc["kind"] == "ConfigMap"
        and doc["metadata"].get("labels", {}).get("factory.jomcgi.dev/garage-claim")
        == "true"
    }
    # The factory-public release mirrors the bucket (HOME-492). This
    # namespace holds no R2 write credential and runs no mirror.
    assert claims == {"hikes-hikes-s3": ("hikes", "readwrite")}
    assert "R2_BUCKET" not in result
    assert "factory-r2-public-mirror" not in result
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
    env = {e["name"]: e for e in container["env"]}
    assert env["SOURCE_BUCKET"]["value"] == "stars"
    assert env["SOURCE_KEY"]["value"] == "walks.json"
    assert {
        e["valueFrom"]["secretKeyRef"]["name"]
        for e in container["env"]
        if "valueFrom" in e
    } == {"hikes-hikes-s3", "hikes-seed"}
    items = {
        d["metadata"]["name"]: d["spec"]["itemPath"]
        for d in documents
        if d["kind"] == "OnePasswordItem"
    }
    assert items["hikes-seed"] == "vaults/k8s-homelab/items/r2-s3-public-read"
    policy = next(
        d
        for d in documents
        if d["kind"] == "CiliumNetworkPolicy"
        and d["metadata"]["name"] == "hikes-bootstrap"
    )
    fqdns = {
        f["matchName"]
        for rule in policy["spec"]["egress"]
        for f in rule.get("toFQDNs", [])
    }
    assert fqdns == {
        "www.walkhighlands.co.uk",
        "7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com",
    }


def test_suspension_removes_the_bootstrap():
    result, documents = render("suspendWriters=true")
    crons = [d for d in documents if d["kind"] == "CronJob"]
    assert len(crons) == 3 and all(d["spec"]["suspend"] for d in crons)
    assert not [d for d in documents if d["kind"] == "Job"]
    assert "hikes-seed" not in result
    assert "r2-s3-public-read" not in result
