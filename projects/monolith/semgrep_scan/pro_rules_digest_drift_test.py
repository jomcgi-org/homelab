"""Drift guard: the monolith's mounted Pro-rule image volumes MUST pin the exact
same OCI digests the fc-invoke guest scans with.

The relay (report.py) recomputes each finding's match_based_id fingerprint, which
requires loading byte-identical rules to the guest. The guest's five Pro packs are
pinned by digest in bazel/semgrep/third_party/semgrep_pro/digests.bzl; the monolith
mounts those same artifacts as native Kubernetes image volumes, pinned by digest in
projects/monolith/deploy/values.yaml (semgrep.proRules). If the two ever drift, the
relay loads a different rule version than the guest and fingerprints silently
mismatch (findings fail to dedup/triage in the Semgrep App), with no error.

This test fails loudly on any drift. When update-semgrep-pro.yaml bumps digests.bzl,
projects/monolith/deploy/values.yaml must be updated in the same change.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import yaml

# The five rule packs the guest's rules_tar bakes (bazel/semgrep/guest/BUILD:
# rules_tar = local_rule_files + pro_{golang,javascript,kubernetes,python,rust}).
# SCA packs are deliberately excluded (the guest excludes them too).
EXPECTED_RULE_DIGEST_KEYS = {
    "rules_golang",
    "rules_javascript",
    "rules_kubernetes",
    "rules_python",
    "rules_rust",
}


def _repo_path(*parts: str) -> Path:
    """Resolve a repo-relative file, in-bazel (TEST_SRCDIR/_main) or standalone."""
    rel = Path(*parts)
    srcdir = os.environ.get("TEST_SRCDIR", "")
    candidate = Path(srcdir) / "_main" / rel
    if candidate.exists():
        return candidate
    # Fallback for a direct (non-bazel) run: this file lives at
    # projects/monolith/semgrep_scan/, so the repo root is three parents up.
    here = Path(__file__).resolve().parents[3] / rel
    if here.exists():
        return here
    raise FileNotFoundError(
        f"file not found at {candidate} or {here} (TEST_SRCDIR={srcdir!r})"
    )


_DIGESTS_BZL = _repo_path(
    "bazel", "semgrep", "third_party", "semgrep_pro", "digests.bzl"
)
_DEPLOY_VALUES = _repo_path("projects", "monolith", "deploy", "values.yaml")


def _load_guest_digests() -> dict[str, str]:
    """Parse SEMGREP_PRO_DIGESTS out of digests.bzl without importing Starlark.

    The file is a module-level dict literal; extract it with the ast module so a
    stray comment or reordering cannot fool a regex.
    """
    source = _DIGESTS_BZL.read_text()
    module = ast.parse(source)
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "SEMGREP_PRO_DIGESTS":
                value = ast.literal_eval(node.value)
                assert isinstance(value, dict)
                return value
    raise AssertionError("SEMGREP_PRO_DIGESTS not found in digests.bzl")


def _load_yaml_mapping(path: Path) -> dict:
    """safe_load a YAML file, asserting it parsed to a mapping."""
    parsed = yaml.safe_load(path.read_text())
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} did not parse to a mapping: {parsed!r}")
    return parsed


def _load_pro_rules() -> list[dict]:
    values = _load_yaml_mapping(_DEPLOY_VALUES)
    return values["semgrep"]["proRules"]


def _digest_of(reference: str) -> str:
    """Return the sha256:... digest suffix of an OCI reference (never a tag)."""
    match = re.search(r"@(sha256:[0-9a-f]{64})$", reference)
    assert match is not None, f"reference is not digest-pinned: {reference}"
    return match.group(1)


def test_files_exist():
    assert _DIGESTS_BZL.is_file(), _DIGESTS_BZL
    assert _DEPLOY_VALUES.is_file(), _DEPLOY_VALUES


def test_pro_rules_cover_exactly_the_guest_rule_packs():
    pro_rules = _load_pro_rules()
    covered = {entry["digestKey"] for entry in pro_rules}
    assert covered == EXPECTED_RULE_DIGEST_KEYS, (
        "monolith proRules must mount exactly the five guest rule packs "
        f"(got {sorted(covered)}, expected {sorted(EXPECTED_RULE_DIGEST_KEYS)})"
    )


def test_every_mounted_digest_matches_the_guest():
    guest = _load_guest_digests()
    for entry in _load_pro_rules():
        key = entry["digestKey"]
        assert key in guest, f"digestKey {key} not present in digests.bzl"
        want = guest[key]
        got = _digest_of(entry["reference"])
        assert got == want, (
            f"Pro-rule digest drift for {key}: values.yaml pins {got} but "
            f"digests.bzl (the guest) has {want}. Update deploy/values.yaml in the "
            "same change as the digests.bzl bump."
        )


def test_reference_repository_matches_the_pack():
    """The reference path must name the rules-<lang> repo for its digestKey."""
    for entry in _load_pro_rules():
        lang = entry["lang"]
        reference = entry["reference"]
        expected_repo = f"ghcr.io/jomcgi/homelab/tools/semgrep-pro/rules-{lang}@"
        assert reference.startswith(expected_repo), (
            f"reference {reference} does not match expected repo {expected_repo}"
        )
        assert entry["digestKey"] == f"rules_{lang}", (
            f"digestKey {entry['digestKey']} does not match lang {lang}"
        )
