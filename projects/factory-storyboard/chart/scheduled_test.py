import json
import os
from pathlib import Path
import subprocess

import yaml


def test_scheduled_writer():
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
    docs = list(yaml.safe_load_all(manifests))
    jobs = [d for d in docs if d and d["kind"] == "CronJob"]
    assert len(jobs) == 1
    job = jobs[0]["spec"]
    assert job["schedule"] == "*/5 * * * *"
    assert job["concurrencyPolicy"] == "Forbid"
    pod = job["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "factory-storyboard"
    assert pod["automountServiceAccountToken"] is False
    assert any(
        d
        and d["kind"] == "ServiceAccount"
        and d["metadata"]["name"] == pod["serviceAccountName"]
        for d in docs
    )
    names = {
        v["valueFrom"]["secretKeyRef"]["name"]
        for v in pod["containers"][0]["env"]
        if "valueFrom" in v
    }
    assert names == {
        "factory-storyboard-factory-public-s3",
        "factory-storyboard-readers",
        "factory-storyboard-db",
    }
    reader = next(
        d
        for d in docs
        if d
        and d["kind"] == "Deployment"
        and d["metadata"]["name"] == "storyboard-task-reader"
    )
    reader_env = {
        v["name"] for v in reader["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert reader_env == {"READER_TOKEN", "OWNER_GRANTS", "UPSTREAM_BEARER"}
    publisher_env = {v["name"] for v in pod["containers"][0]["env"]}
    assert publisher_env.isdisjoint({"UPSTREAM_BEARER", "OWNER_GRANTS"})
    policies = [
        d["spec"]
        for d in docs
        if d
        and d["kind"] == "CiliumNetworkPolicy"
        and d["spec"]["endpointSelector"]["matchLabels"].get("app.kubernetes.io/name")
        == "factory-storyboard"
    ]
    assert policies
    assert "kagent-controller" not in str(policies)
    assert '"8083"' not in json.dumps(policies)
