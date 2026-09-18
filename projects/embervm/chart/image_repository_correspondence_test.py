"""Keep relocated Firecracker image producers aligned with chart defaults."""

from __future__ import annotations

import os
import re
from pathlib import Path

_REGISTRY = "ghcr.io/jomcgi/homelab/"
_CHART_PRODUCERS = {
    ("sandboxPython", "guestImage"): "projects/embervm/firecracker/sandbox/python",
    ("sandboxGo", "guestImage"): "projects/embervm/firecracker/sandbox/go",
    ("sandboxRust", "guestImage"): "projects/embervm/firecracker/sandbox/rust",
    ("sandboxElixir", "guestImage"): "projects/embervm/firecracker/sandbox/elixir",
    ("sandboxOcaml", "guestImage"): "projects/embervm/firecracker/sandbox/ocaml",
    (
        "sandboxJavascript",
        "guestImage",
    ): "projects/embervm/firecracker/sandbox/javascript",
    ("semgrep", "guestImage"): "projects/embervm/firecracker/semgrep/guest",
    (
        "rootfsBuilder",
        "image",
    ): "projects/embervm/firecracker/substrate/rootfs-builder",
}
_EGRESS_PACKAGE = "projects/embervm/firecracker/substrate/egress-proxy"


def _chart_dir() -> Path:
    return Path(__file__).resolve().parent


def _source_repository(package: str) -> str:
    """Read a producer BUILD when this focused test runs outside Bazel."""
    workspace = _chart_dir().parents[2]
    build_text = (workspace / package / "BUILD").read_text()
    repositories = re.findall(r'^\s*repository\s*=\s*"([^"]+)",?$', build_text, re.M)
    if repositories:
        assert len(repositories) == 1, f"{package} has multiple image repositories"
        return repositories[0]
    return _REGISTRY + package


def _provider_repository(package: str) -> str:
    """Read the image rule's repository metadata in Bazel or from source."""
    test_srcdir = os.environ.get("TEST_SRCDIR")
    if not test_srcdir:
        return _source_repository(package)
    repository_file = (
        Path(test_srcdir) / "_main" / package / "image.info.repository"
    )
    assert repository_file.is_file(), f"missing image metadata: {repository_file}"
    return repository_file.read_text().strip()


def _scalar_values(text: str) -> dict[tuple[str, ...], str]:
    """Parse the simple nested scalar paths this correspondence check needs."""
    stack: list[tuple[int, str]] = []
    values: dict[tuple[str, ...], str] = {}
    for line in text.splitlines():
        match = re.match(r"^(\s*)([A-Za-z][A-Za-z0-9]*):(?:\s+(.*))?$", line)
        if not match:
            continue
        indent = len(match.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = match.group(2)
        path = tuple(parent for _, parent in stack) + (key,)
        value = match.group(3)
        if value is None:
            stack.append((indent, key))
        else:
            values[path] = value.strip().strip('"')
    return values


def test_firecracker_image_repositories_match_chart_defaults() -> None:
    values = _scalar_values((_chart_dir() / "values.yaml").read_text())

    for value_path, package in _CHART_PRODUCERS.items():
        chart_repository = values[value_path + ("repository",)]
        producer_repository = _provider_repository(package)
        assert producer_repository == _REGISTRY + package
        assert chart_repository == producer_repository

    # go_image derives this repository from its package path. The chart keeps
    # egress.image empty because helm_chart injects the image.info metadata.
    assert values[("egress", "image")] == "{}"
    assert _provider_repository(_EGRESS_PACKAGE) == _REGISTRY + _EGRESS_PACKAGE

    if not os.environ.get("TEST_SRCDIR"):
        workspace = _chart_dir().parents[2]
        egress_build = (workspace / _EGRESS_PACKAGE / "BUILD").read_text()
        assert not re.search(r"^\s*repository\s*=", egress_build, re.M)
