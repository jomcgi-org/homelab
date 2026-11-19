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
