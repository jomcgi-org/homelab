"""Tests for brick floor label-selector support."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


_NAME = re.compile(r"^  name: (\S+)$", re.MULTILINE)
_SELECTOR = re.compile(r"^      matchLabels:(.+?)^    spec:", re.MULTILINE | re.DOTALL)
_NODE_SELECTOR = re.compile(
    r"^      nodeSelector:(.+?)^      (containers|initContainers):",
    re.MULTILINE | re.DOTALL,
)
_LABELS = re.compile(r"^    labels:(.+?)^    spec:", re.MULTILINE | re.DOTALL)


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

    rendered = {}
    for document in result.stdout.split("\n---"):
        name_match = _NAME.search(document)
        if name_match:
            deployment_name = name_match.group(1)
            rendered[deployment_name] = {
                "doc": document,
                "name": deployment_name,
            }

    return rendered


def _extract_selector_labels(doc: str) -> dict[str, str]:
    match = _SELECTOR.search(doc)
    if not match:
        raise ValueError("Could not find selector matchLabels in document")
    labels_text = match.group(1)
    labels = {}
    for line in labels_text.split("\n"):
        if ":" in line:
            parts = line.split(":", 1)
            key = parts[0].strip()
            value = parts[1].strip().strip("'\"")
            labels[key] = value
    return labels


def _extract_node_selector(doc: str) -> dict[str, str]:
    match = _NODE_SELECTOR.search(doc)
    if not match:
        return {}
    ns_text = match.group(1)
    node_selector = {}
    for line in ns_text.split("\n"):
        if ":" in line:
            parts = line.split(":", 1)
            key = parts[0].strip()
            value = parts[1].strip().strip("'\"")
            node_selector[key] = value
    return node_selector


def test_hostname_floor_renders_with_hostname_selector() -> None:
    tmp_path = Path("/tmp/clay-floor-test-1")
    tmp_path.mkdir(exist_ok=True)

    floors_yaml = """    - node: my-node
      class: test-2gi"""

    rendered = _render_floors(tmp_path, floors_yaml)

    assert len(rendered) == 2, (
        f"Expected 2 deployments, got {len(rendered)}: {list(rendered.keys())}"
    )
    assert "floor-test-embervm-noded-brick-test-2gi-my-node" in rendered

    floor_doc = rendered["floor-test-embervm-noded-brick-test-2gi-my-node"]["doc"]
    node_selector = _extract_node_selector(floor_doc)

    assert "kubernetes.io/hostname" in node_selector
    assert node_selector["kubernetes.io/hostname"] == "my-node"

    selector_labels = _extract_selector_labels(floor_doc)
    assert "embervm.jomcgi.dev/brick-floor" in selector_labels
    assert selector_labels["embervm.jomcgi.dev/brick-floor"] == "my-node"


def test_selector_floor_renders_with_label_selector() -> None:
    tmp_path = Path("/tmp/clay-floor-test-2")
    tmp_path.mkdir(exist_ok=True)

    floors_yaml = """    - name: anchor
      selector:
        homelab.io/anchor: "true"
      class: test-2gi"""

    rendered = _render_floors(tmp_path, floors_yaml)

    assert len(rendered) == 2, (
        f"Expected 2 deployments, got {len(rendered)}: {list(rendered.keys())}"
    )
    assert "floor-test-embervm-noded-brick-test-2gi-anchor" in rendered

    floor_doc = rendered["floor-test-embervm-noded-brick-test-2gi-anchor"]["doc"]
    node_selector = _extract_node_selector(floor_doc)

    assert "homelab.io/anchor" in node_selector
    assert node_selector["homelab.io/anchor"] == "true"
    assert "kubernetes.io/hostname" not in node_selector

    selector_labels = _extract_selector_labels(floor_doc)
    assert "embervm.jomcgi.dev/brick-floor" in selector_labels
    assert selector_labels["embervm.jomcgi.dev/brick-floor"] == "anchor"


def test_both_node_and_selector_fails() -> None:
    tmp_path = Path("/tmp/clay-floor-test-3")
    tmp_path.mkdir(exist_ok=True)

    floors_yaml = """    - node: my-node
      selector:
        homelab.io/anchor: "true"
      class: test-2gi"""

    with pytest.raises(RuntimeError, match="helm template failed"):
        _render_floors(tmp_path, floors_yaml)


def test_neither_node_nor_selector_fails() -> None:
    tmp_path = Path("/tmp/clay-floor-test-4")
    tmp_path.mkdir(exist_ok=True)

    floors_yaml = """    - class: test-2gi"""

    with pytest.raises(RuntimeError, match="helm template failed"):
        _render_floors(tmp_path, floors_yaml)


def test_selector_without_name_fails() -> None:
    tmp_path = Path("/tmp/clay-floor-test-5")
    tmp_path.mkdir(exist_ok=True)

    floors_yaml = """    - selector:
        homelab.io/anchor: "true"
      class: test-2gi"""

    with pytest.raises(RuntimeError, match="helm template failed"):
        _render_floors(tmp_path, floors_yaml)


def test_invalid_name_dns_compliance_fails() -> None:
    tmp_path = Path("/tmp/clay-floor-test-6")
    tmp_path.mkdir(exist_ok=True)

    floors_yaml = """    - name: "INVALID-NAME"
      selector:
        homelab.io/anchor: "true"
      class: test-2gi"""

    with pytest.raises(RuntimeError, match="helm template failed"):
        _render_floors(tmp_path, floors_yaml)


def test_selector_name_appears_in_deployment_labels() -> None:
    tmp_path = Path("/tmp/clay-floor-test-7")
    tmp_path.mkdir(exist_ok=True)

    floors_yaml = """    - name: my-anchor
      selector:
        homelab.io/anchor: "true"
      class: test-2gi"""

    rendered = _render_floors(tmp_path, floors_yaml)
    floor_doc = rendered["floor-test-embervm-noded-brick-test-2gi-my-anchor"]["doc"]

    labels_match = _LABELS.search(floor_doc)
    assert labels_match
    labels_text = labels_match.group(1)

    assert (
        "brick-floor: my-anchor" in labels_text
        or 'brick-floor: "my-anchor"' in labels_text
    )
