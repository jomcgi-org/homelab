"""Tests proving soft-deleted rows are excluded by deleted_at.is_(None) filters.

Covers the surviving deleted_at code paths:

1. gaps.discover_gaps()                        — Gap.deleted_at.is_(None) guard
2. migrate_raw_bucketing._grandfather_atoms()  - Note.deleted_at.is_(None) guard

(The reconciler and research_handler deleted_at guards were retired with
the vault decommission; see ADR 006 and the fileless gap loop.)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.gaps import GAPS_PIPELINE_VERSION, discover_gaps
from knowledge.migrate_raw_bucketing import _grandfather_atoms
from knowledge.models import AtomRawProvenance, Gap, Note, NoteLink


# ---------------------------------------------------------------------------
# Shared fixture — in-memory SQLite with schema stripped (standard pattern)
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_note(session: Session, note_id: str, *, type: str = "atom") -> Note:
    note = Note(
        note_id=note_id,
        path=f"_processed/{note_id}.md",
        title=note_id,
        content_hash=f"hash-{note_id}",
        type=type,
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def _add_link(session: Session, *, src_fk: int, target_id: str) -> None:
    session.add(
        NoteLink(
            src_note_fk=src_fk,
            target_id=target_id,
            target_title=target_id,
            kind="link",
            edge_type=None,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# 1. gaps.discover_gaps — soft-deleted Gap rows are excluded from the
#    "already-exists" lookup so a fresh re-discovery does NOT resurrect them.
# ---------------------------------------------------------------------------


class TestDiscoverGapsDeletedAt:
    def test_soft_deleted_gap_does_not_block_rediscovery(
        self, session: Session
    ) -> None:
        """A soft-deleted Gap for one term must not interfere with discovery of
        a different unresolved term.

        discover_gaps filters deleted_at.is_(None) when loading existing Gap
        rows. Soft-deleted rows are excluded from ``existing_by_note_id`` so
        they don't occupy slots for other terms.
        """
        src = _make_note(session, "source-note")
        # Link to a FRESH term that has no existing Gap row (live or deleted).
        _add_link(session, src_fk=src.id, target_id="brand-new-term")

        # Seed a soft-deleted Gap for a DIFFERENT term — must not interfere
        # with discovery of "brand-new-term".
        soft_deleted = Gap(
            term="old-deleted-term",
            context="",
            note_id="old-deleted-term",
            pipeline_version=GAPS_PIPELINE_VERSION,
            state="discovered",
            deleted_at=_NOW,
        )
        session.add(soft_deleted)
        session.commit()

        created = discover_gaps(session)

        assert created == 1
        live_gaps = (
            session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
        )
        assert len(live_gaps) == 1
        assert live_gaps[0].term == "brand-new-term"


# ---------------------------------------------------------------------------
# 2. migrate_raw_bucketing._grandfather_atoms: soft-deleted atom excluded.
# ---------------------------------------------------------------------------


class TestGrandfatherAtomsDeletedAt:
    def test_soft_deleted_atom_is_not_grandfathered(self, session: Session) -> None:
        """_grandfather_atoms must skip Notes with deleted_at set."""
        deleted_atom = Note(
            note_id="deleted-atom",
            path="_processed/deleted-atom.md",
            title="Deleted Atom",
            content_hash="hash-da",
            type="atom",
            deleted_at=_NOW,
        )
        session.add(deleted_atom)
        session.commit()
        session.refresh(deleted_atom)

        count = _grandfather_atoms(session)

        assert count == 0

        # No provenance row created for the deleted atom.
        prov = session.exec(
            select(AtomRawProvenance).where(
                AtomRawProvenance.atom_fk == deleted_atom.id
            )
        ).all()
        assert prov == []

    def test_live_atom_is_still_grandfathered(self, session: Session) -> None:
        """Live atoms must still receive provenance rows (regression guard)."""
        live_atom = _make_note(session, "live-atom", type="atom")

        count = _grandfather_atoms(session)

        assert count == 1
        prov = session.exec(
            select(AtomRawProvenance).where(AtomRawProvenance.atom_fk == live_atom.id)
        ).all()
        assert len(prov) == 1
