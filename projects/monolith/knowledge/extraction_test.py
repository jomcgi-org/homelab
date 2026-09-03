"""Tests for the Luna knowledge extraction lane."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge.extraction import (
    GARDENER_VERSION,
    RAW_BODY_CAP,
    ExtractionInputMissing,
    ExtractionOutputInvalid,
    _parse_result,
    apply_extraction,
    build_extraction_prompt,
    enqueue_extraction,
)
from knowledge.models import AtomRawProvenance, Dispute, Note, RawInput


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'extraction.db'}")
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _raw(session: Session, source: str, *, extra: dict | None = None) -> RawInput:
    suffix = str(session.exec(select(RawInput)).all().__len__())
    raw = RawInput(
        raw_id=f"raw-{suffix}",
        path=f"raws/raw-{suffix}.md",
        source=source,
        content_hash=f"hash-{suffix}",
        extra=extra or {},
    )
    session.add(raw)
    session.commit()
    session.refresh(raw)
    return raw


class _Embedder:
    async def embed(self, _text):
        return [0.0] * 1024

    async def embed_batch(self, texts):
        return [[0.0] * 1024 for _ in texts]


def test_enqueue_is_one_shot_and_idempotent(session):
    session.execute(
        text(
            """
            CREATE TABLE routine_jobs (
                name TEXT PRIMARY KEY,
                routine_kind TEXT NOT NULL,
                interval_secs INTEGER,
                next_run_at TIMESTAMP,
                payload TEXT,
                created_by TEXT
            )
            """
        )
    )
    session.commit()

    assert enqueue_extraction(session, "raw-1") is True
    assert enqueue_extraction(session, "raw-1") is False
    row = session.execute(text("SELECT * FROM routine_jobs")).one()
    assert row.name == "kg:raw-1"
    assert row.routine_kind == "kg-drain"
    assert row.interval_secs is None
    assert json.loads(row.payload) == {"raw_id": "raw-1"}
    assert row.created_by == "knowledge.extraction"


def _patch_prompt(monkeypatch, body: str, related=None):
    monkeypatch.setattr("knowledge.extraction.raw_store.fetch_raw", lambda _hash: body)
    monkeypatch.setattr("knowledge.extraction.EmbeddingClient", _Embedder)
    monkeypatch.setattr(
        "knowledge.store.KnowledgeStore.search_notes_with_context",
        lambda *_args, **_kwargs: related or [],
    )


@pytest.mark.parametrize(
    ("source", "phrase"),
    [
        ("claude-session", "reusable beyond this session"),
        ("codex-session", "reusable beyond this session"),
        ("ember-session", "reusable beyond this session"),
        ("agent-report", "This is a claim by an agent"),
        ("dispute", "Seek disconfirming AND confirming evidence"),
    ],
)
def test_prompt_uses_source_lens(session, monkeypatch, source, phrase):
    raw = _raw(session, source, extra={"note_id": "old-note", "repo": "acme/repo"})
    _patch_prompt(
        monkeypatch,
        "raw body",
        [
            {
                "note_id": "nearby",
                "title": "Nearby",
                "scope": "repo:acme/repo",
                "verification_state": "verified",
                "snippet": "known detail",
            }
        ],
    )

    prompt = build_extraction_prompt(session, raw)

    assert phrase in prompt
    assert "/workspace/src" in prompt
    assert "- [nearby] Nearby (repo:acme/repo, verified): known detail" in prompt
    assert "reply with exactly one fenced ```json block" in prompt


def test_prompt_caps_body_in_the_middle(session, monkeypatch):
    raw = _raw(session, "agent-report")
    body = "a" * (RAW_BODY_CAP // 2 + 9) + "b" * (RAW_BODY_CAP // 2 + 11)
    _patch_prompt(monkeypatch, body)

    prompt = build_extraction_prompt(session, raw)

    assert re.search(r"\[\.\.\. \d+ chars elided \.\.\.\]", prompt)
    assert "a" * 200 in prompt
    assert prompt.count("b" * 200) >= 1


def test_prompt_rejects_missing_body(session, monkeypatch):
    raw = _raw(session, "agent-report")
    monkeypatch.setattr("knowledge.extraction.raw_store.fetch_raw", lambda _hash: None)

    with pytest.raises(ExtractionInputMissing):
        build_extraction_prompt(session, raw)


def test_parser_uses_last_json_block_and_clamps_fields():
    result = _parse_result(
        '```json\n{"assertions": []}\n```\nignored\n```json\n'
        + json.dumps(
            {
                "assertions": [
                    {
                        "title": "Last",
                        "body": "x" * 21_000,
                        "scope": "repo:acme/repo",
                        "verification_state": "verified",
                        "confidence": 9,
                    }
                ],
                "unknown": True,
            }
        )
        + "\n```"
    )

    assert result.assertions[0].title == "Last"
    assert len(result.assertions[0].body) == 20_000
    assert result.assertions[0].confidence == 1.0


@pytest.mark.parametrize(
    "field",
    [
        {"scope": "everywhere", "verification_state": "verified"},
        {"scope": "repo:acme/repo", "verification_state": "legacy"},
    ],
)
def test_parser_rejects_bad_scope_and_state(field):
    assertion = {"title": "Bad", "body": "body", "confidence": 0.5, **field}
    with pytest.raises(ExtractionOutputInvalid):
        _parse_result(f"```json\n{json.dumps({'assertions': [assertion]})}\n```")


def _result(assertions, dispute_resolution=None):
    return (
        "```json\n"
        + json.dumps(
            {
                "assertions": assertions,
                "dispute_resolution": dispute_resolution,
                "notes": "",
            }
        )
        + "\n```"
    )


def test_apply_writes_atom_provenance_and_scoped_columns(session, monkeypatch):
    raw = _raw(session, "agent-report")
    monkeypatch.setattr("knowledge.atoms.EmbeddingClient", _Embedder)

    applied = apply_extraction(
        session,
        raw.raw_id,
        _result(
            [
                {
                    "title": "Confirmed behavior",
                    "body": "The checkout confirms this behavior.",
                    "scope": "repo:acme/repo",
                    "verification_state": "verified",
                    "confidence": 0.9,
                    "valid_from": "2026-09-01T00:00:00Z",
                    "observed_at": "2026-09-02T00:00:00Z",
                    "tags": ["repo"],
                    "edges": {"related": ["nearby"]},
                    "evidence": ["src/example.py:10"],
                }
            ]
        ),
    )

    assert applied == {
        "raw_id": raw.raw_id,
        "atoms": ["confirmed-behavior"],
        "dispute": None,
        "failed": False,
    }
    note = session.exec(select(Note).where(Note.note_id == "confirmed-behavior")).one()
    assert note.scope == "repo:acme/repo"
    assert note.verification_state == "verified"
    assert note.confidence == 0.9
    provenance = session.exec(
        select(AtomRawProvenance).where(AtomRawProvenance.atom_fk == note.id)
    ).one()
    assert provenance.raw_fk == raw.id
    assert provenance.gardener_version == GARDENER_VERSION


def test_empty_assertions_write_sentinel(session):
    raw = _raw(session, "agent-report")

    applied = apply_extraction(session, raw.raw_id, _result([]))

    assert applied["atoms"] == []
    row = session.exec(
        select(AtomRawProvenance).where(
            AtomRawProvenance.raw_fk == raw.id,
            AtomRawProvenance.derived_note_id == "no-new-notes",
        )
    ).one()
    assert row.gardener_version == GARDENER_VERSION


@pytest.mark.parametrize(
    ("resolution_state", "note_state"),
    [
        ("confirmed", "disputed"),
        ("invalidated", "invalidated"),
        ("rejected", "verified"),
    ],
)
def test_dispute_resolution_updates_open_rows_and_note(
    session, resolution_state, note_state
):
    note = Note(
        note_id="disputed-note",
        path="disputed.md",
        title="Disputed",
        content_hash="hash",
        content="body",
        verification_state="unverified",
    )
    session.add(note)
    session.commit()
    raw = _raw(session, "dispute", extra={"note_id": "disputed-note"})
    dispute = Dispute(note_id="disputed-note", raw_id=raw.raw_id, reason="wrong")
    session.add(dispute)
    session.commit()

    applied = apply_extraction(
        session,
        raw.raw_id,
        _result([], {"state": resolution_state, "rationale": "checked repo"}),
    )

    session.refresh(dispute)
    session.refresh(note)
    assert applied["dispute"] == resolution_state
    assert dispute.state == resolution_state
    assert dispute.resolution == "checked repo"
    assert dispute.resolved_at is not None
    assert note.verification_state == note_state


def test_invalid_output_writes_incrementing_dead_letter(session):
    raw = _raw(session, "agent-report")

    for expected in (1, 2):
        with pytest.raises(ExtractionOutputInvalid):
            apply_extraction(session, raw.raw_id, "not json")
        row = session.exec(
            select(AtomRawProvenance).where(
                AtomRawProvenance.raw_fk == raw.id,
                AtomRawProvenance.derived_note_id == "failed",
            )
        ).one()
        assert row.retry_count == expected
        assert row.error == "missing fenced json block"
