import os
from pathlib import Path
import subprocess

import yaml


def test_deployment_pins_built_digest():
    digest = "sha256:" + "a" * 64
    chart = Path(__file__).resolve().parent
    rendered = subprocess.check_output(
        [
            os.environ["HELM_BIN"],
            "template",
            "alert-relay",
            str(chart),
            "--set",
            f"image.digest={digest}",
        ],
        text=True,
    )
    deployment = next(
        doc for doc in yaml.safe_load_all(rendered) if doc["kind"] == "Deployment"
    )
    pod = deployment["spec"]["template"]["spec"]
    assert (
        pod["containers"][0]["image"]
        == f"ghcr.io/jomcgi/homelab/projects/factory/alert-relay@{digest}"
    )
    assert pod["imagePullSecrets"] == [{"name": "ghcr-imagepull-secret"}]
