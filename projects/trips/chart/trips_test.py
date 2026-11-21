"""Private uploads and public publication use separate capabilities."""

import os
from pathlib import Path
import subprocess

import yaml


def render_trips(*args):
    render = subprocess.check_output(
        [
            os.getenv("HELM_BIN", "helm"),
            "template",
            "trips",
            str(Path(__file__).resolve().parent),
            *args,
        ],
        text=True,
    )
    return {
        (d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(render) if d
    }


def test_staged_mirror_needs_no_r2_credential():
    docs = render_trips()
    assert ("PersistentVolumeClaim", "trips-originals") in docs
    assert docs["CronJob", "trips-trips-public-mirror"]["spec"]["suspend"]
    assert ("OnePasswordItem", "trips-trips-public-mirror-r2") not in docs
    assert ("Deployment", "trips-ingest") not in docs
    assert ("CronJob", "trips-export") not in docs
    assert not any(kind == "HTTPRoute" for kind, _ in docs)


def test_enabled_workloads_keep_private_and_public_capabilities_separate():
    docs = render_trips("--set", "workloadsEnabled=true,image.digest=sha256:fixture")
    route = docs["HTTPRoute", "trips-ingest"]["spec"]
    assert route["hostnames"] == ["factory-trips.jomcgi.dev"]
    assert route["parentRefs"] == [{"name": "edge", "namespace": "edge"}]
    assert route["rules"][0]["matches"] == [
        {"method": "POST", "path": {"type": "Exact", "value": "/api/trips/ingest"}},
        {"method": "GET", "path": {"type": "Exact", "value": "/oauth2/callback"}},
    ]
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
