"""The private review Harness image: the pinned Codex harness plus mandoline."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

import yaml

_MANDOLINE = "usr/local/bin/mandoline"


def _blob(layout: Path, digest: str) -> Path:
    algo, hex_digest = digest.split(":", 1)
    return layout / "blobs" / algo / hex_digest


def _manifest(layout: Path) -> dict:
    index = json.loads((layout / "index.json").read_text())
    (entry,) = index["manifests"]
    manifest = json.loads(_blob(layout, entry["digest"]).read_text())
    if "manifests" in manifest:  # An image index: take its one platform.
        (entry,) = manifest["manifests"]
        manifest = json.loads(_blob(layout, entry["digest"]).read_text())
    return manifest


def _layers(layout: Path) -> list[str]:
    return [layer["digest"] for layer in _manifest(layout)["layers"]]


def test_image_is_the_pinned_harness_plus_one_mandoline_layer() -> None:
    base = _layers(Path(os.environ["BASE_LAYOUT"]))
    image = _layers(Path(os.environ["IMAGE_LAYOUT"]))
    assert image[: len(base)] == base
    assert len(image) == len(base) + 1


def test_image_runs_the_pinned_mandoline() -> None:
    layout = Path(os.environ["IMAGE_LAYOUT"])
    last = _blob(layout, _layers(layout)[-1])
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(last) as layer:
        member = layer.getmember(_MANDOLINE)
        assert member.isfile() and member.mode & 0o111
        layer.extract(member, tmp, filter="data")
        binary = Path(tmp) / _MANDOLINE
        binary.chmod(0o755)
        out = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, check=True
        ).stdout
    assert re.match(r"mandoline 0\.25\.0 ", out), out


def test_base_digest_matches_the_deployed_codex_harness() -> None:
    values = yaml.safe_load(Path(os.environ["DEPLOY_VALUES"]).read_text())
    deployed = values["kagent"]["config"]["harnessImages"]["codex"]
    module = Path(os.environ["MODULE_BAZEL"]).read_text()
    pull = re.search(r'oci\.pull\(\s*name = "codex_harness",(.*?)\n\)', module, re.S)
    assert pull, "MODULE.bazel has no codex_harness pull"
    image = re.search(r'image = "([^"]+)"', pull.group(1)).group(1)
    digest = re.search(r'digest = "([^"]+)"', pull.group(1)).group(1)
    assert deployed == f"{image}@{digest}"
