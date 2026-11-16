"""Factory Authentik server and worker CPU limits render and apply (HOME-131).

A `cpu: null` limit in values-factory.yaml dropped the key in a local
`helm template`, but Argo CD rendered it as a zero limit, which the API
rejects when the request is above it. These tests require an explicit
positive limit at or above the request, and no nulls in the overlay's
resources.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent


def render():
    command = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "authentik",
        str(CHART),
        "--namespace",
        "authentik",
        "-f",
        str(CHART / "values.yaml"),
        "-f",
        str(CHART / "values-factory.yaml"),
    ]
    return subprocess.run(command, capture_output=True, check=True, timeout=120).stdout


def cores(quantity):
    text = str(quantity)
    if text.endswith("m"):
        return int(text[:-1]) / 1000
    return float(text)


@pytest.mark.parametrize("name", ["authentik-server", "authentik-worker"])
def test_factory_cpu_limit_is_positive_and_covers_the_request(name):
    deployments = {
        doc["metadata"]["name"]: doc
        for doc in yaml.safe_load_all(render())
        if doc and doc["kind"] == "Deployment"
    }
    resources = deployments[name]["spec"]["template"]["spec"]["containers"][0][
        "resources"
    ]
    limit = resources.get("limits", {}).get("cpu")
    assert limit not in (None, ""), f"{name} has no CPU limit"
    assert cores(limit) > 0
    assert cores(limit) >= cores(resources["requests"]["cpu"])


def test_factory_overlay_resources_have_no_nulls():
    overlay = yaml.safe_load((CHART / "values-factory.yaml").read_text())

    def nulls(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if value is None and "resources" in path:
                    yield ".".join(path + [key])
                yield from nulls(value, path + [key])

    assert list(nulls(overlay, [])) == []
