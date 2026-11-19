import os
from pathlib import Path
import subprocess

import yaml


def test_writer_and_mirror():
    cmd = [
        os.environ["HELM_BIN"],
        "template",
        "stars-forecast",
        str(Path(__file__).parent),
        "--namespace",
        "stars-forecast",
        "--set",
        "image.digest=sha256:" + "a" * 64,
    ]
    for enabled in [False, True]:
        docs = list(
            yaml.safe_load_all(
                subprocess.check_output(
                    cmd + ["--set", f"refreshEnabled={str(enabled).lower()}"]
                )
            )
        )
        pull = next(
            d
            for d in docs
            if d
            and d["kind"] == "OnePasswordItem"
            and d["metadata"]["name"] == "ghcr-imagepull-secret"
        )
        assert pull["type"] == "kubernetes.io/dockerconfigjson"
        jobs = {
            d["metadata"]["name"]: d["spec"]
            for d in docs
            if d and d["kind"] == "CronJob"
        }
        assert jobs["grid"]["schedule"] == "0 */6 * * *"
        assert ("refresh" in jobs) == enabled
        assert "prune" not in jobs
        if enabled:
            assert jobs["refresh"]["schedule"] == "0 */3 * * *"
            assert jobs["refresh"]["concurrencyPolicy"] == "Forbid"
        mirror = jobs["stars-forecast-stars-forecast-mirror"]
        assert mirror["suspend"]
        # One prefix of the shared public R2 bucket (HOME-492).
        env = {
            e["name"]: e.get("value")
            for e in mirror["jobTemplate"]["spec"]["template"]["spec"]["containers"][0][
                "env"
            ]
        }
        assert (env["R2_BUCKET"], env["R2_PREFIX"]) == (
            "factory-public",
            "stars-forecast",
        )
        item = next(
            d
            for d in docs
            if d
            and d["kind"] == "OnePasswordItem"
            and d["metadata"]["name"] == "stars-forecast-stars-forecast-mirror-r2"
        )
        assert item["spec"]["itemPath"] == (
            "vaults/k8s-homelab/items/factory-r2-public-mirror"
        )
