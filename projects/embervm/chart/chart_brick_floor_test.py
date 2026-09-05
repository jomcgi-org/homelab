"""Tests for brick floor label-selector support."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml


def _chart_dir() -> Path:
    chart = Path(__file__).resolve().parent
    if (chart / "Chart.yaml").exists():
        return chart
    raise RuntimeError("Could not find chart Chart.yaml")


def _render_floors(tmp_path: Path, floors_yaml: str) -> dict[str, dict]:
    overlay = tmp_path / "floor-test.yaml"
    overlay.write_text(
        f"""bricks:
  enabled: true
  syncWaveBase: 2
  syncWaveGroupSize: 1
  classes:
    - name: test-2gi
      resources:
        requests:
          cpu: "1"
          memory: 2Gi
        limits:
          memory: 2Gi
  nodeFloors:
{floors_yaml}
"""
    )

    helm_bin = os.environ.get("HELM_BIN", "helm")
    result = subprocess.run(
        [
            helm_bin,
            "template",
            "floor-test",
            str(_chart_dir()),
            "--values",
            str(overlay),
            "--show-only",
            "templates/brick-deployment.yaml",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")

    # Parse the rendered documents as YAML rather than scraping them with
    # regexes. The regex form silently matched nothing when indentation shifted,
    # which reads as "no nodeSelector" and is indistinguishable from a real
    # template bug.
    rendered = {}
    for document in yaml.safe_load_all(result.stdout):
        if not document or document.get("kind") != "Deployment":
            continue
        rendered[document["metadata"]["name"]] = document

    return rendered


def test_hostname_floor_renders_with_hostname_selector(tmp_path: Path) -> None:

    floors_yaml = """    - node: my-node
      class: test-2gi"""

    rendered = _render_floors(tmp_path, floors_yaml)

    assert len(rendered) == 2, (
        f"Expected 2 deployments, got {len(rendered)}: {list(rendered.keys())}"
    )
    assert "floor-test-embervm-noded-brick-test-2gi-my-node" in rendered

    floor_doc = rendered["floor-test-embervm-noded-brick-test-2gi-my-node"]
    node_selector = floor_doc["spec"]["template"]["spec"]["nodeSelector"]

    assert "kubernetes.io/hostname" in node_selector
    assert node_selector["kubernetes.io/hostname"] == "my-node"

    selector_labels = floor_doc["spec"]["selector"]["matchLabels"]
    assert "embervm.jomcgi.dev/brick-floor" in selector_labels
    assert selector_labels["embervm.jomcgi.dev/brick-floor"] == "my-node"


def test_selector_floor_renders_with_label_selector(tmp_path: Path) -> None:

    floors_yaml = """    - name: anchor
      selector:
        homelab.io/anchor: "true"
      class: test-2gi"""

    rendered = _render_floors(tmp_path, floors_yaml)

    assert len(rendered) == 2, (
        f"Expected 2 deployments, got {len(rendered)}: {list(rendered.keys())}"
    )
    assert "floor-test-embervm-noded-brick-test-2gi-anchor" in rendered

    floor_doc = rendered["floor-test-embervm-noded-brick-test-2gi-anchor"]
    node_selector = floor_doc["spec"]["template"]["spec"]["nodeSelector"]

    assert "homelab.io/anchor" in node_selector
    assert node_selector["homelab.io/anchor"] == "true"
    assert "kubernetes.io/hostname" not in node_selector

    selector_labels = floor_doc["spec"]["selector"]["matchLabels"]
    assert "embervm.jomcgi.dev/brick-floor" in selector_labels
    assert selector_labels["embervm.jomcgi.dev/brick-floor"] == "anchor"


def test_both_node_and_selector_fails(tmp_path: Path) -> None:

    floors_yaml = """    - node: my-node
      selector:
        homelab.io/anchor: "true"
      class: test-2gi"""

    with pytest.raises(RuntimeError, match="helm template failed"):
        _render_floors(tmp_path, floors_yaml)


def test_neither_node_nor_selector_fails(tmp_path: Path) -> None:

    floors_yaml = """    - class: test-2gi"""

    with pytest.raises(RuntimeError, match="helm template failed"):
        _render_floors(tmp_path, floors_yaml)


def test_selector_without_name_fails(tmp_path: Path) -> None:

    floors_yaml = """    - selector:
        homelab.io/anchor: "true"
      class: test-2gi"""

    with pytest.raises(RuntimeError, match="helm template failed"):
        _render_floors(tmp_path, floors_yaml)


def test_invalid_name_dns_compliance_fails(tmp_path: Path) -> None:

    floors_yaml = """    - name: "INVALID-NAME"
      selector:
        homelab.io/anchor: "true"
      class: test-2gi"""

    with pytest.raises(RuntimeError, match="helm template failed"):
        _render_floors(tmp_path, floors_yaml)


def test_selector_name_appears_in_deployment_labels(tmp_path: Path) -> None:

    floors_yaml = """    - name: my-anchor
      selector:
        homelab.io/anchor: "true"
      class: test-2gi"""

    rendered = _render_floors(tmp_path, floors_yaml)
    floor_doc = rendered["floor-test-embervm-noded-brick-test-2gi-my-anchor"]

    labels = floor_doc["metadata"]["labels"]
    assert labels["embervm.jomcgi.dev/brick-floor"] == "my-anchor"
