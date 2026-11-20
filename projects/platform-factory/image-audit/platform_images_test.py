"""Audit workloads rendered with the factory Applications' Helm values."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from check_image_digests import violations

HERE = Path(__file__).absolute().parents[1]
ROOT = HERE.parents[1]


@pytest.mark.parametrize(
    "component",
    [
        "argocd",
        "authentik",
        "cert-manager",
        "edge",
        "kargo",
        "onepassword",
        "otel-collector",
        "renovate",
    ],
)
def test_factory_platform_images(component):
    spec = yaml.safe_load((HERE / component / "application.yaml").read_text())["spec"]
    sources = spec.get("sources") or [spec["source"]]
    chart = next(
        source for source in sources if (ROOT / source["path"] / "Chart.yaml").exists()
    )
    helm = chart.get("helm", {})
    command = [
        str(Path(os.environ["HELM_BIN"]).absolute())
        if "HELM_BIN" in os.environ
        else "helm",
        "template",
        helm.get("releaseName", component),
        ".",
        "-n",
        spec["destination"]["namespace"],
    ]
    for values in helm.get("valueFiles", []):
        command.extend(["-f", values])
    rendered = subprocess.check_output(command, cwd=ROOT / chart["path"], text=True)
    assert not (
        missing := [
            msg
            for doc in yaml.safe_load_all(rendered)
            if doc
            for msg in violations(doc)
        ]
    ), missing
