import os
from pathlib import Path
import subprocess

import yaml


def test_writer_schedules_and_credentials():
    result = subprocess.check_output(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "hikes",
            str(Path(__file__).parent),
            "--namespace",
            "hikes",
            "--set",
            "image.digest=sha256:" + "a" * 64,
        ],
        text=True,
    )
    documents = [doc for doc in yaml.safe_load_all(result) if doc]
    claims = {
        doc["data"]["secretName"]: (doc["data"]["bucket"], doc["data"]["access"])
        for doc in documents
        if doc["kind"] == "ConfigMap"
        and doc["metadata"].get("labels", {}).get("factory.jomcgi.dev/garage-claim")
        == "true"
    }
    assert claims == {
        "hikes-hikes-s3": ("hikes", "readwrite"),
        "hikes-hikes-mirror-s3": ("hikes", "read"),
    }
    jobs = {
        d["metadata"]["name"]: d["spec"] for d in documents if d["kind"] == "CronJob"
    }
    assert all(job["suspend"] for job in jobs.values())
    # One prefix of the shared public R2 bucket (HOME-492).
    mirror = jobs["hikes-hikes-mirror"]["jobTemplate"]["spec"]["template"]["spec"]
    env = {e["name"]: e.get("value") for e in mirror["containers"][0]["env"]}
    assert (env["R2_BUCKET"], env["R2_PREFIX"]) == ("factory-public", "hikes")
    items = {
        d["metadata"]["name"]: d["spec"]["itemPath"]
        for d in documents
        if d["kind"] == "OnePasswordItem"
    }
    assert items["hikes-hikes-mirror-r2"] == (
        "vaults/k8s-homelab/items/factory-r2-public-mirror"
    )
    assert jobs["hikes-refresh"]["schedule"] == "20 */2 * * *"
    assert jobs["hikes-prune"]["schedule"] == "40 * * * *"
    for mode in ("catalogue", "refresh", "prune"):
        job = jobs[f"hikes-{mode}"]
        container = job["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        assert any(
            m["mountPath"] == "/var/lock/hikes" for m in container["volumeMounts"]
        )
        assert container["args"] == [mode]
        assert {e["valueFrom"]["secretKeyRef"]["name"] for e in container["env"]} == {
            "hikes-hikes-s3"
        }
