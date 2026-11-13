"""Offline routing checks for source-specific knowledge extraction lenses."""

import pytest

from knowledge.extraction import _lens
from knowledge.models import RawInput


def _raw(source: str) -> RawInput:
    return RawInput(
        raw_id=f"raw-{source}",
        path=f"raws/{source}.md",
        source=source,
        content_hash="hash",
        extra={"repo": "jomcgi-org/homelab", "note_id": "disputed-routing-note-729"},
    )


@pytest.mark.parametrize("source", ["claude-session", "ember-session", "codex-session"])
def test_session_lens_routes_shared_rules(monkeypatch, source):
    sentinel = "SESSION_RULES_SENTINEL_481"
    monkeypatch.setattr("knowledge.extraction._SESSION_BEHAVIOUR_RULES", sentinel)

    lens = _lens(_raw(source))

    if source == "codex-session":
        assert sentinel in lens
    else:
        assert lens == sentinel


@pytest.mark.parametrize("source", ["repo-diff", "agent-report", "dispute"])
def test_non_session_lens_excludes_shared_rules(monkeypatch, source):
    sentinel = "SESSION_RULES_SENTINEL_481"
    monkeypatch.setattr("knowledge.extraction._SESSION_BEHAVIOUR_RULES", sentinel)

    assert sentinel not in _lens(_raw(source))


def test_dispute_lens_receives_note_id():
    raw = _raw("dispute")

    assert raw.extra["note_id"] in _lens(raw)
