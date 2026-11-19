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
        docs = yaml.safe_load_all(
            subprocess.check_output(
                cmd + ["--set", f"refreshEnabled={str(enabled).lower()}"]
            )
        )
        jobs = {
            d["metadata"]["name"]: d["spec"]
            for d in docs
            if d and d["kind"] == "CronJob"
        }
        assert jobs["grid"]["schedule"] == "0 */6 * * *"
        assert ("refresh" in jobs) == enabled
        assert ("prune" in jobs) == enabled
        if enabled:
            assert jobs["refresh"]["schedule"] == "0 */3 * * *"
            assert jobs["prune"]["schedule"] == "50 * * * *"
        assert jobs["stars-forecast-stars-forecast-mirror"]["suspend"]
