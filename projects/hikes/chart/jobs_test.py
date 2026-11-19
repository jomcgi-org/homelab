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
    jobs = {
        d["metadata"]["name"]: d["spec"]
        for d in yaml.safe_load_all(result)
        if d and d["kind"] == "CronJob"
    }
    assert all(job["suspend"] for job in jobs.values())
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
