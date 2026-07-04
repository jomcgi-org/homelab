"""Guard (ADR 023 phase 6b): the kloak: secret placeholders must not drift.

The credential-isolation swap works only if the placeholder a guest carries has
a matching entry in the fc-invoke egress catalog. Two files must therefore agree
on the exact set of placeholder strings:

  - projects/monolith/deploy/values.yaml  -> goosecracker.tiers.<tier>.<ENV>
    values that the runner injects into a guest microVM (the placeholders a guest
    HOLDS).
  - projects/firecracker/substrate/deploy/values.yaml -> egress.secrets[].placeholder
    (the placeholders the sidecar SWAPS for a real credential on the egressTo host).

If a guest holds a placeholder the catalog lacks, the real request leaves the
guest carrying the inert placeholder (a broken credentialed call, and a design
that no longer does what it claims). If the catalog carries a placeholder no tier
holds, it is dead config that should be pruned. This test fails CI on either kind
of drift, comparing the placeholder STRINGS (the env-var NAMES intentionally
differ between the two files, e.g. OPENROUTER_KEY vs OPENROUTER_API_KEY).

Reading values.yaml directly (not a rendered manifest) keeps the test free of a
Helm toolchain; the relevant blocks are plain literals there.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

# kloak:<kind>:<ulid-ish> — e.g. "kloak:gh:01JZX8K3N7Q2M5R9W4T6Y0F1B8".
_PLACEHOLDER_RE = re.compile(r"^kloak:[a-z]+:[A-Za-z0-9]+$")


def _values_path(*parts: str) -> Path:
    """Resolve a repo-relative values.yaml, in-bazel (TEST_SRCDIR) or standalone."""
    rel = Path(*parts)
    srcdir = os.environ.get("TEST_SRCDIR", "")
    candidate = Path(srcdir) / "_main" / rel
    if candidate.exists():
        return candidate
    # Fallback for a direct (non-bazel) run from the repo root: this file lives at
    # projects/monolith/, so the repo root is two parents up.
    here = Path(__file__).resolve().parent.parent.parent / rel
    if here.exists():
        return here
    raise FileNotFoundError(
        f"values.yaml not found at {candidate} or {here} (TEST_SRCDIR={srcdir!r})"
    )


def _load(*parts: str) -> dict:
    return yaml.safe_load(_values_path(*parts).read_text())


def _monolith_placeholders() -> set[str]:
    """kloak: placeholders any goose tier hands to a guest."""
    values = _load("projects", "monolith", "deploy", "values.yaml")
    tiers = (values.get("goosecracker") or {}).get("tiers") or {}
    found: set[str] = set()
    for env in tiers.values():
        if not isinstance(env, dict):
            continue
        for val in env.values():
            if isinstance(val, str) and val.startswith("kloak:"):
                found.add(val)
    return found


def _catalog_placeholders() -> set[str]:
    """placeholders the fc-invoke egress sidecar knows how to swap."""
    values = _load("projects", "firecracker", "substrate", "deploy", "values.yaml")
    secrets = (values.get("egress") or {}).get("secrets") or []
    return {
        entry["placeholder"]
        for entry in secrets
        if isinstance(entry, dict) and "placeholder" in entry
    }


def test_placeholders_match():
    held = _monolith_placeholders()
    swappable = _catalog_placeholders()
    unswappable = sorted(held - swappable)
    orphaned = sorted(swappable - held)
    assert held == swappable, (
        "kloak placeholder drift between the monolith goose tiers and the "
        "fc-invoke egress catalog.\n"
        f"  held by a guest but MISSING from the swap catalog (would leak/break): {unswappable}\n"
        f"  orphan catalog entries with no holder (dead config, prune): {orphaned}\n"
        "Keep projects/monolith/deploy/values.yaml (goosecracker.tiers) and "
        "projects/firecracker/substrate/deploy/values.yaml (egress.secrets) in sync."
    )


def test_placeholders_wellformed():
    for placeholder in _monolith_placeholders() | _catalog_placeholders():
        assert _PLACEHOLDER_RE.match(placeholder), (
            f"malformed kloak placeholder {placeholder!r}; expected kloak:<kind>:<id>"
        )
