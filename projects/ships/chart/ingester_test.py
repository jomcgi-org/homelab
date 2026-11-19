import os
from pathlib import Path
import subprocess

import yaml


def test_shadow_activation():
    # The chart default runs zero replicas until HOME-483 sets enabled=true.
    for overrides, replicas in (([], 0), (["--set", "enabled=true"], 1)):
        result = subprocess.check_output(
            [
                os.getenv("HELM_BIN", "helm"),
                "template",
                "ships",
                str(Path(__file__).parent),
                "--set",
                "image.digest=sha256:" + "0" * 64,
                *overrides,
            ]
        )
        spec = next(
            doc["spec"]
            for doc in yaml.safe_load_all(result)
            if doc["kind"] == "Deployment"
        )
        assert spec["replicas"] == replicas
        assert spec["strategy"]["type"] == "Recreate"
        container = spec["template"]["spec"]["containers"][0]
        assert container["env"][-1]["value"] == "false"


def test_sized_database():
    rendered = subprocess.check_output(
        [
            os.getenv("HELM_BIN", "helm"),
            "template",
            "ships",
            str(Path(__file__).parent),
            "--set",
            "image.digest=sha256:" + "0" * 64,
            "--set",
            "database.storage=5Gi,database.resources.requests.memory=128Mi",
        ]
    )
    docs = list(yaml.safe_load_all(rendered))
    cluster = next(d["spec"] for d in docs if d["kind"] == "Cluster")
    assert cluster["storage"]["size"] == "5Gi"
    (ref,) = cluster["bootstrap"]["initdb"]["postInitApplicationSQLRefs"][
        "configMapRefs"
    ]
    config = next(
        d
        for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == ref["name"]
    )
    assert "CREATE TABLE ships.heat_cells_historical" in config["data"][ref["key"]]
