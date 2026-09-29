"""Postgres regression: re-draining a raw with no new notes stays idempotent.

The partial unique index knowledge.atom_raw_provenance_pending rejects a
second (raw_fk, 'no-new-notes', gardener_version) row. The no-new-notes
write must therefore be INSERT ... ON CONFLICT DO NOTHING, and the proof
must run against real Postgres: SQLite does not enforce the partial index
the same way, so a SQLite-only test cannot show the UniqueViolation.
"""

from __future__ import annotations

import json
from uuid import uuid4

from sqlmodel import Session, create_engine, select

from knowledge.extraction import EXTRACTION_VERSION, apply_extraction
from knowledge.models import AtomRawProvenance, RawInput


def _empty_result_text() -> str:
    return (
        "```json\n"
        + json.dumps(
            {
                "assertions": [],
                "dispute_resolution": None,
                "doc_drift": [],
                "notes": "",
            }
        )
        + "\n```"
    )


def test_no_new_notes_rerun_leaves_single_pending_row(pg):
    engine = create_engine(pg.url)
    try:
        suffix = uuid4().hex[:12]
        raw_id = f"pg-no-new-notes-{suffix}"
        with Session(engine) as session:
            raw = RawInput(
                raw_id=raw_id,
                path=f"raws/{raw_id}.md",
                source="agent-report",
                content_hash=raw_id,
                extra={},
            )
            session.add(raw)
            session.commit()
            session.refresh(raw)
            raw_pk = raw.id

        with Session(engine) as session:
            first = apply_extraction(session, raw_id, _empty_result_text())
        assert first["failed"] is False
        assert first["replayed"] is False

        # A retry or correction pass re-enters apply_extraction with passes
        # already at 1. The correction lane (passes == 1) proceeds past the
        # replay guard, so this is the call that used to raise
        # UniqueViolation on the pending partial unique index.
        with Session(engine) as session:
            second = apply_extraction(
                session, raw_id, _empty_result_text(), correction=True
            )
        assert second["failed"] is False
        assert second["replayed"] is False

        with Session(engine) as session:
            rows = session.exec(
                select(AtomRawProvenance).where(
                    AtomRawProvenance.raw_fk == raw_pk,
                )
            ).all()
            assert len(rows) == 1
            assert rows[0].atom_fk is None
            assert rows[0].derived_note_id == "no-new-notes"
            assert rows[0].gardener_version == EXTRACTION_VERSION
            raw = session.exec(
                select(RawInput).where(RawInput.raw_id == raw_id)
            ).one()
            assert int((raw.extra or {}).get("extraction_passes", 0)) >= 1
    finally:
        engine.dispose()
