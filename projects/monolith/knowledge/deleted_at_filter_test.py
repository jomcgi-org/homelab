"""Tests proving soft-deleted rows are excluded by deleted_at.is_(None) filters.

Covers the five code paths changed in commits 681b23a8b..83fbcec21:

1. gaps.discover_gaps()                    — Gap.deleted_at.is_(None) guard
2. gaps.classify_gaps()                    — Gap.deleted_at.is_(None) guard
3. gardener._resolve_pending_provenance()  — Note.deleted_at.is_(None) guard
4. gardener._distill_completed_tasks()     — Note.deleted_at.is_(None) guard
5. reconciler._project_gap_frontmatter()   — Gap.deleted_at.is_(None) guard
6. migrate_raw_bucketing._grandfather_atoms() — Note.deleted_at.is_(None) guard
7. research_handler._sweep_and_select_candidates() — Gap.deleted_at.is_(None) guard
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.frontmatter import ParsedFrontmatter
from knowledge.gaps import GAPS_PIPELINE_VERSION, classify_gaps, discover_gaps
from knowledge.gardener import GARDENER_VERSION, Gardener
from knowledge.migrate_raw_bucketing import _grandfather_atoms
from knowledge.models import AtomRawProvenance, Gap, Note, NoteLink, RawInput
from knowledge.reconciler import _project_gap_frontmatter
from knowledge.research_handler import _sweep_and_select_candidates


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


@pytest.fixture(name="engine")
def engine_fixture():
    """Engine-level fixture required by _sweep_and_select_candidates."""
    _engine = create_engine(
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
        SQLModel.metadata.create_all(_engine)
        yield _engine
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


def _make_raw(session: Session, raw_id: str) -> RawInput:
    raw = RawInput(
        raw_id=raw_id,
        path=f"_raw/2026/05/26/{raw_id}.md",
        source="test",
        content="Body.",
        content_hash=f"hash-raw-{raw_id}",
    )
    session.add(raw)
    session.commit()
    session.refresh(raw)
    return raw


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
        self, session: Session, tmp_path: Path
    ) -> None:
        """A soft-deleted Gap for one term must not interfere with discovery of
        a different unresolved term.

        discover_gaps filters deleted_at.is_(None) when loading existing Gap
        rows (~line 224).  Soft-deleted rows are excluded from
        ``existing_by_note_id`` so they don't occupy slots for other terms,
        and are excluded from ``gap_candidates`` so they are never tombstoned.

        Note: a soft-deleted Gap *cannot* be re-inserted under the same
        note_id / term due to the UniqueConstraint on both columns.  The
        filter's value lies in preventing tombstoning and in not letting a
        stale soft-deleted row shadow an unrelated fresh discovery.
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

        # discover_gaps must discover "brand-new-term" and return created=1,
        # unaffected by the soft-deleted gap for the unrelated term.
        created = discover_gaps(session, tmp_path)

        assert created == 1
        live_gaps = (
            session.execute(
                select(Gap).where(Gap.deleted_at.is_(None))
            )
            .scalars()
            .all()
        )
        assert len(live_gaps) == 1
        assert live_gaps[0].term == "brand-new-term"


# ---------------------------------------------------------------------------
# 2. gaps.classify_gaps — soft-deleted gaps in state='discovered' are skipped.
# ---------------------------------------------------------------------------


class TestClassifyGapsDeletedAt:
    def test_soft_deleted_discovered_gap_is_not_classified(
        self, session: Session, tmp_path: Path
    ) -> None:
        """classify_gaps must not process gaps whose deleted_at is set.

        The filter on ~line 407 selects only live (deleted_at IS NULL) rows
        in state='discovered'.  A soft-deleted discovered gap must stay
        unclassified.
        """
        # Insert one live gap via discover_gaps and one soft-deleted gap.
        live_src = _make_note(session, "live-src")
        _add_link(session, src_fk=live_src.id, target_id="live-term")
        discover_gaps(session, tmp_path)  # creates live Gap for "live-term"

        deleted_gap = Gap(
            term="deleted-term",
            context="",
            note_id="deleted-term",
            pipeline_version=GAPS_PIPELINE_VERSION,
            state="discovered",
            deleted_at=_NOW,
        )
        session.add(deleted_gap)
        session.commit()

        def classifier(term: str, _context: str) -> str:
            return "external"

        classified = classify_gaps(session, classifier=classifier)

        # Only the live gap should be classified.
        assert classified == 1

        session.refresh(deleted_gap)
        assert deleted_gap.gap_class is None
        assert deleted_gap.state == "discovered"
        assert deleted_gap.classified_at is None


# ---------------------------------------------------------------------------
# 3. gardener._resolve_pending_provenance — soft-deleted Note is not resolved.
# ---------------------------------------------------------------------------


class TestResolvePendingProvenanceDeletedAt:
    def test_soft_deleted_note_is_not_resolved(
        self, session: Session, tmp_path: Path
    ) -> None:
        """_resolve_pending_provenance must skip Notes with deleted_at set.

        The filter on ~line 288 means a soft-deleted note's note_id lookup
        returns None, so the pending AtomRawProvenance row stays unresolved
        (derived_note_id is not cleared, resolved count stays 0).
        """
        # A soft-deleted atom note.
        deleted_note = Note(
            note_id="deleted-atom",
            path="_processed/deleted-atom.md",
            title="Deleted Atom",
            content_hash="hash-deleted",
            type="atom",
            deleted_at=_NOW,
        )
        session.add(deleted_note)
        session.commit()
        session.refresh(deleted_note)

        # Pending provenance: atom_fk=None, derived_note_id pointing at the
        # deleted note.  raw_fk is set to satisfy the NOT NULL constraint
        # (at least one of atom_fk/raw_fk must be non-None).
        raw = _make_raw(session, "raw-for-provenance")
        pending = AtomRawProvenance(
            raw_fk=raw.id,
            derived_note_id="deleted-atom",
            gardener_version="v-test",
        )
        session.add(pending)
        session.commit()
        session.refresh(pending)

        gardener = Gardener(vault_root=tmp_path, session=session)
        resolved = gardener._resolve_pending_provenance()

        # The soft-deleted note must not match → row stays unresolved.
        assert resolved == 0
        session.refresh(pending)
        assert pending.derived_note_id == "deleted-atom"
        assert pending.atom_fk is None  # not linked to the deleted note

    def test_live_note_is_resolved_normally(
        self, session: Session, tmp_path: Path
    ) -> None:
        """Live notes must still be resolved (regression guard)."""
        live_note = _make_note(session, "live-atom")
        raw = _make_raw(session, "raw-live")
        pending = AtomRawProvenance(
            raw_fk=raw.id,
            derived_note_id="live-atom",
            gardener_version="v-test",
        )
        session.add(pending)
        session.commit()
        session.refresh(pending)

        gardener = Gardener(vault_root=tmp_path, session=session)
        resolved = gardener._resolve_pending_provenance()

        assert resolved == 1
        # _resolve_pending_provenance modifies objects in-memory but does not
        # commit; the production caller (garden()) commits afterward.  Commit
        # here so session.refresh() reads the updated DB state.
        session.commit()
        session.refresh(pending)
        assert pending.atom_fk == live_note.id
        assert pending.derived_note_id is None


# ---------------------------------------------------------------------------
# 4. gardener._distill_completed_tasks — soft-deleted active Note is skipped.
# ---------------------------------------------------------------------------


class TestDistillCompletedTasksDeletedAt:
    @pytest.mark.asyncio
    async def test_soft_deleted_done_task_is_not_distilled(
        self, session: Session, tmp_path: Path
    ) -> None:
        """_distill_completed_tasks must skip Notes with deleted_at set.

        The filter on ~line 656 excludes soft-deleted notes from the active
        tasks query, so a done task that has been soft-deleted should never
        trigger distillation.
        """
        rel_path = "_processed/done-deleted.md"
        vault_file = tmp_path / rel_path
        vault_file.parent.mkdir(parents=True, exist_ok=True)
        vault_file.write_text(
            "---\nid: done-deleted\ntitle: Done Deleted\n"
            "type: active\nstatus: done\n---\nBody.\n"
        )
        note = Note(
            note_id="done-deleted",
            path=rel_path,
            title="Done Deleted",
            content_hash="hash-done-deleted",
            type="active",
            extra={"status": "done"},
            deleted_at=_NOW,
        )
        session.add(note)
        session.commit()

        gardener = Gardener(vault_root=tmp_path, session=session)
        mock_distill = AsyncMock()
        gardener._distill_one = mock_distill  # type: ignore[method-assign]

        distilled, failed = await gardener._distill_completed_tasks()

        assert distilled == 0
        assert failed == 0
        mock_distill.assert_not_called()


# ---------------------------------------------------------------------------
# 5. reconciler._project_gap_frontmatter — soft-deleted Gap is not projected.
# ---------------------------------------------------------------------------


class TestProjectGapFrontmatterDeletedAt:
    def test_soft_deleted_gap_is_treated_as_missing(self, session: Session) -> None:
        """_project_gap_frontmatter must return early when the Gap is soft-deleted.

        The select on ~line 444 filters Gap.deleted_at.is_(None), so a
        soft-deleted Gap row will not be found and the function treats it as
        "stub without a Gap row" (early return).  The gap's state must remain
        unchanged.
        """
        gap = Gap(
            term="deleted-gap",
            context="",
            note_id="deleted-gap",
            pipeline_version=GAPS_PIPELINE_VERSION,
            state="discovered",
            deleted_at=_NOW,
        )
        session.add(gap)
        session.commit()
        session.refresh(gap)

        meta = ParsedFrontmatter(
            note_id="deleted-gap",
            type="gap",
            status="classified",
            extra={"gap_class": "external"},
        )

        # Must not raise and must not modify the soft-deleted gap row.
        _project_gap_frontmatter(session, "deleted-gap", meta)

        session.refresh(gap)
        assert gap.state == "discovered"  # unchanged — soft-deleted row ignored
        assert gap.gap_class is None  # not projected


# ---------------------------------------------------------------------------
# 6. migrate_raw_bucketing._grandfather_atoms — soft-deleted atom excluded.
# ---------------------------------------------------------------------------


class TestGrandfatherAtomsDeletedAt:
    def test_soft_deleted_atom_is_not_grandfathered(self, session: Session) -> None:
        """_grandfather_atoms must skip Notes with deleted_at set.

        The filter on ~line 133 selects only live (deleted_at IS NULL) atoms.
        A soft-deleted atom must not receive a pre-migration provenance row.
        """
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


# ---------------------------------------------------------------------------
# 7. research_handler._sweep_and_select_candidates — soft-deleted Gap excluded.
# ---------------------------------------------------------------------------


class TestSweepAndSelectCandidatesDeletedAt:
    def test_soft_deleted_external_classified_gap_is_not_selected(self, engine) -> None:
        """_sweep_and_select_candidates must not return soft-deleted gaps.

        The filter on ~line 142 includes Gap.deleted_at.is_(None).  A gap
        that matches gap_class='external', state='classified' but has
        deleted_at set must not appear in the candidates list.
        """
        with Session(engine) as session:
            deleted_gap = Gap(
                term="soft-deleted-external",
                context="",
                gap_class="external",
                state="classified",
                note_id="soft-deleted-external",
                pipeline_version=GAPS_PIPELINE_VERSION,
                deleted_at=_NOW,
            )
            session.add(deleted_gap)
            session.commit()

        _stuck_count, candidates = _sweep_and_select_candidates(engine)

        candidate_ids = [g.note_id for g in candidates]
        assert "soft-deleted-external" not in candidate_ids

    def test_live_external_classified_gap_is_still_selected(self, engine) -> None:
        """Live eligible gaps must still be returned (regression guard)."""
        with Session(engine) as session:
            live_gap = Gap(
                term="live-external",
                context="",
                gap_class="external",
                state="classified",
                note_id="live-external",
                pipeline_version=GAPS_PIPELINE_VERSION,
                deleted_at=None,
            )
            session.add(live_gap)
            session.commit()

        _stuck_count, candidates = _sweep_and_select_candidates(engine)

        candidate_ids = [g.note_id for g in candidates]
        assert "live-external" in candidate_ids
