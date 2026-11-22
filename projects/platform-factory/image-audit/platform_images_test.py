# invariant: PLAT-11
# check_image_digests is a sibling source of this test target.
# gazelle:ignore check_image_digests,check_image_digests.violations
"""Audit workloads rendered with the factory Applications' Helm values."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from check_image_digests import images, violations

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
    missing = [
        msg for doc in yaml.safe_load_all(rendered) if doc for msg in violations(doc)
    ]
    assert not missing, missing


# Kustomize Applications whose manifests hold workloads. The remote charts
# (CloudNativePG, External Secrets, Cilium) cannot render without network;
# HOME-582 and HOME-583 move their render coverage to their vendoring.
KUSTOMIZE_APPLICATIONS = [
    "authentik",
    "cfgate",
    "factory-db",
    "garage",
    "grafana",
    "grimoire-data",
    "hindsight",
    "hindsight-discord",
    "kagent-postgres",
    "prometheus",
    "substrate",
    "t3",
]


@pytest.mark.parametrize("application", KUSTOMIZE_APPLICATIONS)
def test_factory_kustomize_images(application):
    path = HERE / application / "resources"
    rendered = subprocess.check_output(
        [
            str(Path(os.environ["KUSTOMIZE_BIN"]).absolute())
            if "KUSTOMIZE_BIN" in os.environ
            else "kustomize",
            "build",
            str(path),
        ],
        text=True,
    )
    docs = [doc for doc in yaml.safe_load_all(rendered) if doc]
    assert any(list(images(doc.get("spec", {}))) for doc in docs), "no workload"
    missing = [msg for doc in docs for msg in violations(doc)]
    assert not missing, missing


@pytest.mark.parametrize(
    "manifest",
    [
        "factory-db/resources/cluster.yaml",
        "grimoire-data/resources/database.yaml",
        "kagent-postgres/resources/cluster.yaml",
    ],
)
def test_postgres_operand_image(manifest):
    docs = list(yaml.safe_load_all((HERE / manifest).read_text()))
    assert any(doc and doc["kind"] == "Cluster" for doc in docs)
    assert not (missing := [msg for doc in docs if doc for msg in violations(doc)]), (
        missing
    )
