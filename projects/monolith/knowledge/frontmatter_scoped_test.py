"""Tests for scoped assertion frontmatter."""

from datetime import datetime, timezone

from knowledge.frontmatter import ParsedFrontmatter, parse
from knowledge.notes import _serialize_frontmatter


def test_promoted_scoped_keys_parse_with_lenient_timestamps():
    raw = """---
id: scoped-note
scope: repo:owner/repo
verification_state: verified
confidence: 0.75
valid_from: 2026-09-01
valid_until: '2026-10-01T12:30:00+02:00'
observed_at: '2026-09-02T08:00:00Z'
custom: retained
---
body
"""

    metadata, body = parse(raw)

    assert metadata.scope == "repo:owner/repo"
    assert metadata.verification_state == "verified"
    assert metadata.confidence == 0.75
    assert metadata.valid_from == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert metadata.valid_until is not None
    assert metadata.valid_until.utcoffset().total_seconds() == 7200
    assert metadata.observed_at == datetime(2026, 9, 2, 8, tzinfo=timezone.utc)
    assert metadata.extra == {"custom": "retained"}
    assert body == "body\n"


def test_invalid_timestamps_warn_and_become_none(caplog):
    raw = """---
valid_from: yesterday-ish
valid_until: [not, a, date]
observed_at: 42
---
body
"""

    with caplog.at_level("WARNING", logger="monolith.knowledge.frontmatter"):
        metadata, _ = parse(raw)

    assert metadata.valid_from is None
    assert metadata.valid_until is None
    assert metadata.observed_at is None
    assert sum("date" in record.message for record in caplog.records) == 3


def test_invalid_verification_state_warns_and_leaves_default(caplog):
    with caplog.at_level("WARNING", logger="monolith.knowledge.frontmatter"):
        metadata, _ = parse("---\nverification_state: trusted\n---\nbody\n")

    assert metadata.verification_state is None
    assert any("verification_state" in record.message for record in caplog.records)


def test_scoped_fields_round_trip_through_serializer():
    metadata = ParsedFrontmatter(
        note_id="round-trip",
        title="Round Trip",
        scope="environment:production",
        verification_state="disputed",
        confidence=0.4,
        valid_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
        valid_until=datetime(2026, 9, 30, tzinfo=timezone.utc),
        observed_at=datetime(2026, 9, 2, 6, 30, tzinfo=timezone.utc),
    )

    serialized = _serialize_frontmatter(metadata, "Body")
    reparsed, body = parse(serialized)

    assert reparsed.scope == metadata.scope
    assert reparsed.verification_state == metadata.verification_state
    assert reparsed.confidence == metadata.confidence
    assert reparsed.valid_from == metadata.valid_from
    assert reparsed.valid_until == metadata.valid_until
    assert reparsed.observed_at == metadata.observed_at
    assert body.strip() == "Body"


def test_serializer_omits_legacy_verification_state():
    serialized = _serialize_frontmatter(
        ParsedFrontmatter(note_id="legacy", verification_state="legacy"), "Body"
    )

    assert "verification_state" not in serialized
