"""Static regression coverage for colocated monolith package classification."""

from __future__ import annotations

import re
from pathlib import Path

MONOLITH_ROOT = Path(__file__).resolve().parent
PACKAGE_DIRS = [
    "agent",
    "agent_sessions",
    "app",
    "artifact",
    "auth",
    "campsites",
    "chat",
    "chat_public",
    "cluster",
    "core",
    "demos",
    "dr_jobs",
    "e2e",
    "ember_public",
    "faas",
    "framework",
    "goosecracker",
    "grimoire",
    "grimoire_chat",
    "hikes",
    "home",
    "knowledge",
    "moving",
    "observability",
    "sandbox",
    "scheduler",
    "scripts",
    "semgrep_scan",
    "shared",
    "ships",
    "shotter",
    "stars",
    "swarm",
    "trips",
    "updates",
    "worldcup"
]
PUBLIC_PACKAGE_DIRS = [
    "agent_sessions",
    "app",
    "artifact",
    "campsites",
    "chat_public",
    "core",
    "dr_jobs",
    "ember_public",
    "faas",
    "framework",
    "grimoire",
    "grimoire_chat",
    "hikes",
    "home",
    "knowledge",
    "observability",
    "semgrep_scan",
    "shared",
    "ships",
    "stars",
    "trips",
    "worldcup"
]


def _build(package: str) -> str:
    return (MONOLITH_ROOT / package / "BUILD").read_text()


def _rule(build: str, name: str) -> str:
    match = re.search(
        rf'py_library\(\n    name = "{re.escape(name)}",[\s\S]*?\n\)',
        build,
    )
    assert match is not None, f"missing py_library {name}"
    return match.group(0)


def test_package_classification_is_colocated_and_private_by_default() -> None:
    central = (MONOLITH_ROOT / "BUILD").read_text()

    assert "_BACKEND_SRCS" not in central
    for package in PACKAGE_DIRS:
        assert f"# gazelle:exclude {package}\n" not in central
        build = _build(package)
        assert 'package(default_visibility = ["//visibility:private"])' in build

    backend = _rule(central, "monolith_backend")
    assert "srcs =" not in backend
    assert "glob(" not in backend
    assert backend.count('"//projects/monolith/') >= 30


def test_public_membership_uses_only_explicit_markers() -> None:
    for package in PUBLIC_PACKAGE_DIRS:
        public = _rule(_build(package), "public")
        assert 'tags = ["monolith-public"]' in public
        assert (
            'visibility = ["//projects/monolith/public_backend:__pkg__"]'
            in public
        )

    for package in set(PACKAGE_DIRS) - set(PUBLIC_PACKAGE_DIRS):
        assert 'name = "public"' not in _build(package)

    aggregator = _rule(_build("public_backend"), "monolith_public_backend")
    for package in PUBLIC_PACKAGE_DIRS:
        assert f'"//projects/monolith/{package}:public"' in aggregator
    assert ":pkg_" not in aggregator
    assert "srcs =" not in aggregator
