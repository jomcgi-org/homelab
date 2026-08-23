"""Couple hypervisorEpoch to the vendored Firecracker version (#4409).

Firecracker snapshot formats are version-locked, and the base signature has no
hypervisor input, so a Firecracker bump that forgets to move hypervisorEpoch
leaves every workload base restoring a stale snapshot under the new binary
(every dispatch fails, discovered post-merge: #4389, #4406). Nothing tied the two
values together before this test. It reads the `firecracker_version` attr of the
`kata_firecracker_archive` repo in MODULE.bazel and asserts the chart's
hypervisorEpoch names exactly that version, so CI goes red the moment they drift.

The epoch participates in the base signature via initEnv, so the value itself
is load-bearing: changing it rebuilds every image-lane base. The pinned-value
test guards the coupling from changing the epoch as a side effect.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

# Shape of a valid epoch: `fc-<firecracker version>` with an optional `-rN`
# re-key suffix for moving the epoch without moving the binary (r2 was the
# post-roll re-key after the v1.16.1 race, see the values.yaml comment).
_EPOCH = re.compile(r"^fc-(?P<version>v\d+\.\d+\.\d+)(?:-r(?P<rekey>\d+))?$")

# The epoch live today. Changing how the epoch is derived must not change this
# value; a deliberate epoch move updates this constant in the same PR.
_CURRENT_EPOCH = "fc-v1.16.1-r2"


def _chart_values() -> Path:
    return Path(__file__).resolve().parent / "values.yaml"


def _module_bazel() -> Path:
    return Path(os.environ["MODULE_BAZEL"])


def _firecracker_version() -> str:
    text = _module_bazel().read_text()
    block = re.search(
        r"^kata_firecracker_archive\(\n(?P<body>.*?)^\)$",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert block, "MODULE.bazel has no kata_firecracker_archive(...) call"
    attr = re.search(
        r'^\s*firecracker_version = "(?P<v>[^"]+)",$', block["body"], re.MULTILINE
    )
    assert attr, "kata_firecracker_archive has no firecracker_version attr"
    return attr["v"]


def _hypervisor_epoch() -> str:
    text = _chart_values().read_text()
    matches = re.findall(r'^hypervisorEpoch: "(?P<epoch>[^"]+)"$', text, re.MULTILINE)
    assert len(matches) == 1, (
        f"expected exactly one top-level hypervisorEpoch, got {matches}"
    )
    return matches[0]


def test_firecracker_version_is_pinned() -> None:
    version = _firecracker_version()
    assert re.fullmatch(r"v\d+\.\d+\.\d+", version), version


def test_epoch_names_the_vendored_firecracker_version() -> None:
    epoch = _hypervisor_epoch()
    parsed = _EPOCH.match(epoch)
    assert parsed, f"hypervisorEpoch {epoch!r} is not of the form fc-vX.Y.Z[-rN]"
    version = _firecracker_version()
    assert parsed["version"] == version, (
        f"hypervisorEpoch {epoch!r} names Firecracker {parsed['version']} but MODULE.bazel "
        f"kata_firecracker_archive pins {version}. Snapshot formats are version-locked: "
        f"move hypervisorEpoch to fc-{version} in the same PR as the binary (and see #4407 "
        "for the roll-window sequencing caveat)."
    )


def test_epoch_value_unchanged_by_coupling() -> None:
    assert _hypervisor_epoch() == _CURRENT_EPOCH


@pytest.mark.parametrize(
    "epoch",
    ["fc-v1.16.1", "fc-v1.16.1-r2", "fc-v1.16.1-r10"],
)
def test_epoch_pattern_accepts_rekey_suffix(epoch: str) -> None:
    assert _EPOCH.match(epoch)["version"] == "v1.16.1"


@pytest.mark.parametrize(
    "epoch",
    ["v1.16.1", "fc-1.16.1", "fc-v1.16.1-rc1", "fc-v1.16.1r2", "fc-v1.16"],
)
def test_epoch_pattern_rejects_malformed(epoch: str) -> None:
    assert _EPOCH.match(epoch) is None
