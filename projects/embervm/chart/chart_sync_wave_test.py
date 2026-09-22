"""Render-test grouped ArgoCD sync waves for brick Deployments."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


_NAME = re.compile(r"^  name: (\S+)$", re.MULTILINE)
_WAVE = re.compile(r'^    argocd\.argoproj\.io/sync-wave: "(\d+)"$', re.MULTILINE)
_FIRST_INIT = re.compile(
    r"^      initContainers:\n(?:^        #.*\n)*^        - name: (\S+)$",
    re.MULTILINE,
)


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
    # HELM_BIN is a Bazel-pinned runfile and argv is passed without a shell.
    result = subprocess.run(
        [  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
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
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")

    rendered_waves: dict[str, int] = {}
    for document in result.stdout.split("\n---"):
        name = _NAME.search(document)
        wave = _WAVE.search(document)
        if name and wave:
            rendered_waves[name.group(1)] = int(wave.group(1))
            first_init = _FIRST_INIT.search(document)
            assert first_init, f"{name.group(1)} rendered without init containers"
            assert first_init.group(1) == "wait-for-scratch-generation"

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


def test_scratch_generation_marker_is_the_final_prep_step() -> None:
    script = (_chart_dir() / "files" / "scratch-prep.sh").read_text()

    mount = script.index('host mount -t "$image_type"')
    fstab = script.index('reconcile_fstab "$image_type"')
    marker = script.rindex("write_marker")
    assert mount < fstab < marker


def test_wildcard_daemonset_does_not_inherit_scratch_marker_gate() -> None:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    # HELM_BIN is a Bazel-pinned runfile and argv is passed without a shell.
    result = subprocess.run(
        [  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
            helm_bin,
            "template",
            "scratch-wildcard-test",
            str(_chart_dir()),
            "--set",
            "scratchPrep.enabled=true",
            "--show-only",
            "templates/noded-deployment.yaml",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "wait-for-scratch-generation" not in result.stdout
    assert "EMBERVM_NODED_SCRATCH_GENERATION_PATH" not in result.stdout


def test_scratch_prep_marker_path_tracks_nvme_root() -> None:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    marker = "/custom/embervm-scratch/.scratch-generation"
    # HELM_BIN is a Bazel-pinned runfile and argv is passed without a shell.
    result = subprocess.run(
        [  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
            helm_bin,
            "template",
            "scratch-path-test",
            str(_chart_dir()),
            "--set",
            "scratchPrep.enabled=true",
            "--set",
            "noded.firecracker.nvmeRoot=/custom/embervm-scratch",
            "--show-only",
            "templates/scratch-prep-daemonset.yaml",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert f'value: "{marker}"' in result.stdout
    assert f'test -s "{marker}"' in result.stdout
