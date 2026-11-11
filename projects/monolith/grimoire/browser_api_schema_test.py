"""The committed TypeScript contract must match the live response models."""

from __future__ import annotations

import os
from pathlib import Path

from grimoire.browser_api_schema import render

_REL = Path("projects/monolith/web/grimoire/src/api/schema.ts")


def _committed() -> Path:
    for root in (
        Path(os.environ.get("TEST_SRCDIR", "")) / "_main",
        Path(__file__).resolve().parents[3],
    ):
        if (root / _REL).exists():
            return root / _REL
    raise FileNotFoundError(f"{_REL} not found in runfiles or the checkout")


def test_committed_schema_matches_the_api():
    assert _committed().read_text() == render(), (
        "web/grimoire/src/api/schema.ts is stale. From projects/monolith run: "
        "python -m grimoire.browser_api_schema > web/grimoire/src/api/schema.ts"
    )


def test_schema_covers_only_browser_routed_operations():
    text = render()
    assert '"GET /lobby"' in text
    assert '"POST /campaigns/{campaign_id}/sessions/{session_id}/events"' in text
    # Library and operator routes are not routed to the browser.
    assert "/books" not in text
    assert "/alias-candidates" not in text
    assert "/join-links/inspect" not in text
