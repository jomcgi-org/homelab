"""Cross-check (issue #4994 review round, STPA pass): the baked egress
policy must be a subset of the chart's global allowlist.

projects/embervm/runtimes/shotter/etc/shotter-egress.json (baked into the
guest image, the PRIMARY control, ADR embervm/035 section 4) and
egress.internal.allowlist in projects/embervm/deploy/values.yaml (the
sidecar's global catalog) are two independently hand-maintained lists with
no build-time link between them. Nothing stops a future image rebuild
widening the baked allowlist with no chart-side signal at all: the sidecar
would simply refuse the new destination at runtime, silently, with no test
having caught the drift beforehand.

This asserts SUBSET, not equality: the chart legitimately carries entries
for other egress-enabled workloads (today the claude runtime) that the
shotter guest's own policy has no reason to know about.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml


def _repo_path(*parts: str) -> Path:
    """Resolve a repo-relative path, in-bazel (TEST_SRCDIR) or standalone."""
    rel = Path(*parts)
    candidate = Path(os.environ.get("TEST_SRCDIR", "")) / "_main" / rel
    if candidate.exists():
        return candidate
    # Direct run: this file lives at projects/embervm/runtimes/shotter/.
    here = Path(__file__).resolve().parents[4] / rel
    if here.exists():
        return here
    raise FileNotFoundError(f"{rel} not found at {candidate} or {here}")


def _baked_allowlist() -> list[str]:
    contents = json.loads(
        _repo_path(
            "projects/embervm/runtimes/shotter/etc/shotter-egress.json"
        ).read_text()
    )
    return contents.get("allowlist") or []


def _chart_allowlist() -> list[str]:
    values = yaml.safe_load(
        _repo_path("projects/embervm/deploy/values.yaml").read_text()
    )
    return ((values.get("egress") or {}).get("internal") or {}).get("allowlist") or []


def test_baked_allowlist_is_a_subset_of_the_chart_allowlist() -> None:
    baked = _baked_allowlist()
    assert baked, "the baked shotter-egress.json allowlist is empty"

    chart = set(_chart_allowlist())
    missing = [destination for destination in baked if destination not in chart]
    assert not missing, (
        f"shotter-egress.json allows {missing} but egress.internal.allowlist "
        "in projects/embervm/deploy/values.yaml does not carry it. A guest "
        "image rebuild must not be able to widen what the guest may dial "
        "with no matching chart-side entry."
    )
