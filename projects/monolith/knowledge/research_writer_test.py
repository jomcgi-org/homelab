"""Tests for the vault writers (research raws and quarantine drafts)."""

from __future__ import annotations

from pathlib import Path

import yaml

from knowledge.research_agent import Claim, SourceEntry
from knowledge.research_writer import quarantine, write_research_raw


def _read_fm(path: Path) -> dict:
    text = path.read_text()
    _, fm, _ = text.split("---", 2)
    return yaml.safe_load(fm)


def test_write_research_raw_minimal(tmp_path: Path) -> None:
    out = write_research_raw(
        vault_root=tmp_path,
        slug="foo",
        title="Foo",
        summary="A short summary.",
        supported_claims=[
            Claim(text="Foo is a thing.", source_refs=("https://example.com/a",))
        ],
        sources=[SourceEntry(tool="WebFetch", ref="https://example.com/a")],
        agent_model="sonnet",
        researched_at="2026-04-27T12:00:00Z",
    )
    fm = _read_fm(out)
    assert fm["type"] == "research"
    assert fm["agent_model"] == "sonnet"
    assert fm["pipeline_version"] == "research-pipeline@v2"
    assert fm["claims_supported"] == 1
    assert fm["sources"] == [{"tool": "WebFetch", "ref": "https://example.com/a"}]
    body = out.read_text()
    assert "## Summary" in body
    assert "Foo is a thing." in body
    assert "_[https://example.com/a]_" in body
    assert "## Sources" in body
    assert "- WebFetch: https://example.com/a" in body


def test_write_research_raw_multi_source(tmp_path: Path) -> None:
    out = write_research_raw(
        vault_root=tmp_path,
        slug="bar",
        title="Bar",
        summary="A multi-source summary.",
        supported_claims=[
            Claim(
                text="Bar combines vault and web.",
                source_refs=("https://example.com/b", "vault:notes/bar.md"),
            ),
        ],
        sources=[
            SourceEntry(tool="WebFetch", ref="https://example.com/b"),
            SourceEntry(tool="Read", ref="vault:notes/bar.md"),
        ],
        agent_model="sonnet",
        researched_at="2026-04-27T12:00:00Z",
    )
    body = out.read_text()
    assert "_[https://example.com/b, vault:notes/bar.md]_" in body
    assert "- WebFetch: https://example.com/b" in body
    assert "- Read: vault:notes/bar.md" in body


def test_quarantine_writes_failed_research(tmp_path: Path) -> None:
    out = quarantine(
        vault_root=tmp_path,
        slug="foo",
        attempt=2,
        summary="A summary that didn't pan out.",
        pre_filter_claims=[
            Claim(
                text="claim Sonnet emitted",
                source_refs=("https://h1", "https://h2"),
            ),
            Claim(text="another would-be claim", source_refs=()),
        ],
        sources=[SourceEntry(tool="WebSearch", ref="foo query")],
        agent_model="sonnet",
        researched_at="2026-04-27T12:00:00Z",
    )
    fm = _read_fm(out)
    assert fm["type"] == "failed_research"
    assert fm["id"] == "foo-2"
    assert fm["attempt"] == 2
    assert fm["agent_model"] == "sonnet"
    assert fm["pipeline_version"] == "research-pipeline@v2"
    assert fm["claims_emitted"] == 2
    body = out.read_text()
    assert "# Failed research draft (attempt 2)" in body
    assert "## Claims (pre-filter)" in body
    assert "- claim Sonnet emitted _[https://h1, https://h2]_" in body
    assert "- another would-be claim _[(no refs)]_" in body
    # Old "Claims (Qwen)" header must not appear.
    assert "## Claims (Qwen)" not in body


def test_quarantine_path_format(tmp_path: Path) -> None:
    out = quarantine(
        vault_root=tmp_path,
        slug="thing",
        attempt=1,
        summary="x",
        pre_filter_claims=[],
        sources=[],
        agent_model="sonnet",
        researched_at="2026-04-27T12:00:00Z",
    )
    assert out.parent.name == "_failed_research"
    assert out.name == "thing-1.md"


def test_research_raw_path_format(tmp_path: Path) -> None:
    out = write_research_raw(
        vault_root=tmp_path,
        slug="thing",
        title="Thing",
        summary="x",
        supported_claims=[],
        sources=[],
        agent_model="sonnet",
        researched_at="2026-04-27T12:00:00Z",
    )
    assert out.parent.name == "research"
    assert out.parent.parent.name == "_inbox"
    assert out.name == "thing.md"


def test_research_raw_sets_visibility_public(tmp_path: Path) -> None:
    out_path = write_research_raw(
        vault_root=tmp_path,
        slug="dora-metrics",
        title="DORA Metrics",
        summary="...",
        supported_claims=[],
        sources=[],
        agent_model="sonnet",
        researched_at="2026-05-07T12:00:00Z",
    )
    text = out_path.read_text()
    assert "visibility: public" in text
    fm = _read_fm(out_path)
    assert fm["visibility"] == "public"


def test_failed_research_does_not_set_visibility(tmp_path: Path) -> None:
    """Quarantined drafts are forensic; visibility is irrelevant."""
    out_path = quarantine(
        vault_root=tmp_path,
        slug="bad",
        attempt=1,
        summary="...",
        pre_filter_claims=[],
        sources=[],
        agent_model="sonnet",
        researched_at="2026-05-07T12:00:00Z",
    )
    assert "visibility" not in out_path.read_text()
