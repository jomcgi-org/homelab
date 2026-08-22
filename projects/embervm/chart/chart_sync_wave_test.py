"""Render-test grouped ArgoCD sync waves for brick Deployments."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


_NAME = re.compile(r"^  name: (\S+)$", re.MULTILINE)
_WAVE = re.compile(r'^    argocd\.argoproj\.io/sync-wave: "(\d+)"$', re.MULTILINE)


def _chart_dir() -> Path:
    chart = Path(__file__).resolve().parent
    if (chart / "Chart.yaml").exists():
        return chart
    raise RuntimeError("Could not find chart Chart.yaml")


def _render_waves(tmp_path: Path, group_size: int) -> tuple[list[int], list[int]]:
    classes = "\n".join(
        f"""    - name: class-{index}
      resources:
        requests:
          cpu: \"1\"
          memory: 1Gi
        limits:
          memory: 1Gi"""
        for index in range(5)
    )
    floors = "\n".join(
        f"""    - node: node-{index}
      class: class-0"""
        for index in range(4)
    )
    overlay = tmp_path / f"sync-wave-group-{group_size}.yaml"
    overlay.write_text(
        f"""bricks:
  enabled: true
  syncWaveBase: 2
  syncWaveGroupSize: {group_size}
  classes:
{classes}
  nodeFloors:
{floors}
"""
    )

    helm_bin = os.environ.get("HELM_BIN", "helm")
    result = subprocess.run(
        [
            helm_bin,
            "template",
            "sync-wave-test",
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

    rendered_waves: dict[str, int] = {}
    for document in result.stdout.split("\n---"):
        name = _NAME.search(document)
        wave = _WAVE.search(document)
        if name and wave:
            rendered_waves[name.group(1)] = int(wave.group(1))

    class_waves = [
        rendered_waves[f"sync-wave-test-embervm-noded-brick-class-{index}"]
        for index in range(5)
    ]
    floor_waves = [
        rendered_waves[f"sync-wave-test-embervm-noded-brick-class-0-node-{index}"]
        for index in range(4)
    ]
    assert len(rendered_waves) == 9, "expected only five class and four floor bricks"
    return class_waves, floor_waves


@pytest.mark.parametrize(
    ("group_size", "expected_classes", "expected_floors"),
    [
        (1, [2, 3, 4, 5, 6], [7, 8, 9, 10]),
        (3, [2, 2, 2, 3, 3], [4, 4, 4, 5]),
    ],
)
def test_brick_sync_waves_are_grouped_without_class_floor_overlap(
    tmp_path: Path,
    group_size: int,
    expected_classes: list[int],
    expected_floors: list[int],
) -> None:
    class_waves, floor_waves = _render_waves(tmp_path, group_size)

    assert class_waves == expected_classes
    assert floor_waves == expected_floors
    assert min(floor_waves) > max(class_waves)
