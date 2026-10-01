"""Guard (#6642): the factory toolchains ship in the claude guest and stay on PATH.

The factory retro found ~26% of sessions bootstrapping a toolchain (pip install
pytest, downloading helm or Go) before doing any work. The fix is the package
list in apko.yaml, and it has three ways to rot silently:

  * someone swaps a pinned name for the bare one (`helm-3` -> `helm` moves the
    guest to helm 4.x, `pnpm~10` -> `pnpm` to the pnpm 11 provider, which wants a
    newer node than the one shipped);
  * apko.yaml is edited without a relock, so the image build resolves the OLD
    lock, or the lock is hand-edited and drops a tool;
  * guest-init's forced PATH loses /usr/bin, and every tool above is installed
    there but unreachable from a session;
  * the repo moves past the streams the guest ships (go.mod's go directive,
    MODULE.bazel's node and pnpm), so apko.yaml's "matches the repo" rationale
    goes false and, for go, GOTOOLCHAIN=auto downloads a toolchain on first use.

Every expected value below is a literal, deliberately NOT read back from the file
under test: a guard that derives its expectation from the thing it guards passes
whatever that thing says.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path

import yaml

# The names exactly as written in apko.yaml's contents.packages.
TOOLCHAIN_PACKAGES = [
    "helm-3",
    "go-1.26",
    "nodejs-20",
    "pnpm~10",
    "py3.12-pytest",
    "py3.12-pyyaml",
]

# Unpinned spellings that resolve to a different major than the repo uses. None of
# these may appear as a package entry.
FORBIDDEN_BARE_PACKAGES = ["helm", "go", "nodejs", "pnpm", "pytest", "pyyaml"]

# Lock package name -> required version prefix ("" = any). The lock records what
# the constraint resolved to, so `pnpm~10` is the lock entry `pnpm`.
EXPECTED_LOCK_ENTRIES = {
    "helm-3": "3.",
    "go-1.26": "1.26.",
    "nodejs-20": "20.",
    "pnpm": "10.",
    "py3.12-pytest": "",
    "py3.12-pyyaml": "",
}

# The pnpm 11 provider must not be resolved: it wants Node >= 22.13.
FORBIDDEN_LOCK_ENTRIES = ["pnpm-11", "pnpm-11.8", "pnpm-12", "helm", "helm-4"]

PATH_PATTERN = re.compile(r'"PATH"\s*:\s*"([^"]+)"')


def _repo_path(*parts: str) -> Path:
    """Resolve a repo-relative path, in-bazel (TEST_SRCDIR) or standalone."""
    rel = Path(*parts)
    candidate = Path(os.environ.get("TEST_SRCDIR", "")) / "_main" / rel
    if candidate.exists():
        return candidate
    # Direct run: this file lives at projects/embervm/runtimes/claude/.
    here = Path(__file__).resolve().parents[4] / rel
    if here.exists():
        return here
    raise FileNotFoundError(f"{rel} not found at {candidate} or {here}")


APKO_YAML = "projects/embervm/runtimes/claude/apko.yaml"
APKO_LOCK = "projects/embervm/runtimes/claude/apko.lock.json"
GUEST_INIT_MAIN = "projects/embervm/runtimes/claude/guest-init/cmd/main.go"
GO_MOD = "go.mod"
MODULE_BAZEL = "MODULE.bazel"

GO_DIRECTIVE = re.compile(r"^go\s+(\S+)\s*$", re.MULTILINE)
GO_TOOLCHAIN_DIRECTIVE = re.compile(r"^toolchain\s+(\S+)\s*$", re.MULTILINE)
NODE_VERSION = re.compile(r'node\.toolchain\([^)]*node_version\s*=\s*"([^"]+)"')
PNPM_VERSION = re.compile(r'pnpm\.pnpm\([^)]*pnpm_version\s*=\s*"([^"]+)"')


def _packages() -> list[str]:
    config = yaml.safe_load(_repo_path(APKO_YAML).read_text())
    return config["contents"]["packages"]


def _lock() -> dict:
    return json.loads(_repo_path(APKO_LOCK).read_text())


def test_apko_yaml_lists_the_toolchain_packages():
    packages = _packages()
    missing = [name for name in TOOLCHAIN_PACKAGES if name not in packages]
    assert not missing, f"toolchain packages missing from apko.yaml: {missing}"


def test_apko_yaml_has_no_unpinned_toolchain_spellings():
    packages = _packages()
    bare = [name for name in FORBIDDEN_BARE_PACKAGES if name in packages]
    assert not bare, (
        f"unpinned toolchain packages in apko.yaml: {bare}. They resolve to a "
        "different major than the repo uses (helm 4, pnpm 11); use the pinned "
        "names in TOOLCHAIN_PACKAGES."
    )


def test_lock_resolves_each_toolchain_for_x86_64():
    entries = _lock()["contents"]["packages"]
    for name, version_prefix in EXPECTED_LOCK_ENTRIES.items():
        hits = [
            p for p in entries if p["name"] == name and p["architecture"] == "x86_64"
        ]
        assert hits, f"apko.lock.json has no x86_64 entry for {name}; relock"
        assert all(p["version"].startswith(version_prefix) for p in hits), (
            f"{name} resolved to {[p['version'] for p in hits]}, "
            f"expected a {version_prefix!r} release"
        )


def test_lock_does_not_resolve_the_wrong_major():
    names = {p["name"] for p in _lock()["contents"]["packages"]}
    wrong = [name for name in FORBIDDEN_LOCK_ENTRIES if name in names]
    assert not wrong, f"apko.lock.json resolved the wrong stream: {wrong}"


def test_lock_checksum_matches_apko_yaml():
    """apko stamps sha256-<base64 raw digest> of apko.yaml into the lock.

    An apko.yaml edit (even a comment) without a relock leaves the old checksum,
    and the image build then fails or, worse, ships the stale resolution.
    """
    digest = hashlib.sha256(_repo_path(APKO_YAML).read_bytes()).digest()
    expected = "sha256-" + base64.b64encode(digest).decode()
    assert _lock()["config"]["checksum"] == expected, (
        "apko.lock.json is stale against apko.yaml; relock with "
        "`apko lock projects/embervm/runtimes/claude/apko.yaml`"
    )
    assert _lock()["config"]["name"] == APKO_YAML


def test_guest_init_forced_path_contains_usr_bin():
    """guest-init FORCES PATH; every toolchain above installs into /usr/bin."""
    source = _repo_path(GUEST_INIT_MAIN).read_text()
    match = PATH_PATTERN.search(source)
    assert match, "guest-init no longer sets PATH in setDefaultEnv"
    assert "/usr/bin" in match.group(1).split(":"), (
        f"guest-init PATH is {match.group(1)!r}; the toolchains live in /usr/bin"
    )


def test_go_mod_stays_within_the_shipped_go_stream():
    """The guest ships go-1.26. A go.mod that moves past it (or adds a newer
    `toolchain` line) makes Wolfi's GOTOOLCHAIN=auto download a toolchain on the
    first go command of every session, the bootstrap #6642 removes."""
    source = _repo_path(GO_MOD).read_text()
    directive = GO_DIRECTIVE.search(source)
    assert directive, "go.mod has no go directive"
    assert directive.group(1).startswith("1.26."), (
        f"go.mod says go {directive.group(1)}; the guest ships go-1.26. Bump "
        "`go-1.26` in apko.yaml (and relock) in the same change."
    )
    toolchain = GO_TOOLCHAIN_DIRECTIVE.search(source)
    assert toolchain is None or toolchain.group(1).startswith("go1.26."), (
        f"go.mod toolchain line is {toolchain.group(1) if toolchain else None!r}; "
        "the guest ships go-1.26"
    )


def test_module_bazel_node_and_pnpm_match_the_shipped_streams():
    """apko.yaml ships nodejs-20 and pnpm~10 because MODULE.bazel pins those."""
    source = _repo_path(MODULE_BAZEL).read_text()
    node = NODE_VERSION.search(source)
    assert node, "MODULE.bazel has no node.toolchain(node_version = ...)"
    assert node.group(1).startswith("20."), (
        f"MODULE.bazel node_version is {node.group(1)}; the guest ships nodejs-20"
    )
    pnpm = PNPM_VERSION.search(source)
    assert pnpm, "MODULE.bazel has no pnpm.pnpm(pnpm_version = ...)"
    assert pnpm.group(1).startswith("10."), (
        f"MODULE.bazel pnpm_version is {pnpm.group(1)}; the guest ships pnpm~10"
    )
