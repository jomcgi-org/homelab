"""Private uploads and public publication use separate capabilities."""

import os
from pathlib import Path
import subprocess

import yaml


def test_staged_workloads():
    render = subprocess.check_output(
        [
            os.getenv("HELM_BIN", "helm"),
            "template",
            "trips",
            str(Path(__file__).resolve().parent),
            "--set",
            "workloadsEnabled=true,image.digest=sha256:fixture",
        ],
        text=True,
    )
    docs = {
        (d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(render) if d
    }
    assert ("HTTPRoute", "trips-ingest") not in docs
    export = docs["CronJob", "trips-export"]["spec"]
    assert (export["schedule"], export["timeZone"], export["suspend"]) == (
        "0 3 * * *",
        "Etc/UTC",
        True,
    )
    assert docs["CronJob", "trips-trips-public-mirror"]["spec"]["suspend"]
    pod = export["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["volumes"][0]["persistentVolumeClaim"]["readOnly"]
    assert pod["containers"][0]["envFrom"] == [
        {"secretRef": {"name": "trips-trips-public-s3"}}
    ]
    ingest = docs["Deployment", "trips-ingest"]["spec"]["template"]["spec"]
    assert "envFrom" not in ingest["containers"][0]
