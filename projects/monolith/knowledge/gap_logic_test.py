"""Unit tests for knowledge.gaps — split_csv, _rewrite_sources, and full
lifecycle scenarios not covered by gap_lifecycle_test.py.

Gap lifecycle happy-path and basic error tests live in
``gap_lifecycle_test.py`` (registered separately in BUILD). This file
fills the remaining coverage:

* ``split_csv`` — all paths including None / empty / whitespace edge cases
* ``_rewrite_sources`` — direct unit tests for dry_run mode and OSError paths
* Phase-A discardable-stub rewriting with KNOWLEDGE_GAPS_REWRITE_DISCARDABLE
* Phase-B tombstoning of discardable gaps with no remaining source refs
* ``classify_gaps`` edge case: no pending gaps + no classifier (no warning)
* ``answer_gap`` multiple-collision suffix (-2, -3 …)

Fixture style mirrors ``gap_lifecycle_test.py``:
  - in-memory SQLite with schema-strip (no real Postgres needed)
  - real filesystem via pytest ``tmp_path``
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.gap_stubs import RESEARCHING_DIR
from knowledge.gaps import (
    GAPS_PIPELINE_VERSION,
    _rewrite_sources,
    answer_gap,
    classify_gaps,
    discover_gaps,
    list_review_queue,
    split_csv,
)
from knowledge.models import Gap, Note, NoteLink


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session; strips Postgres schema names so DDL works."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas: dict[str, str] = {}
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


def _make_note(
    session: Session,
    note_id: str,
    *,
    title: str | None = None,
    rel_path: str | None = None,
) -> Note:
    note = Note(
        note_id=note_id,
        path=rel_path or f"_processed/{note_id}.md",
        title=title or note_id,
        content_hash=f"hash-{note_id}",
        type="atom",
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def _add_body_link(session: Session, *, src_fk: int, target_id: str) -> None:
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


def _write_stub(tmp_path: Path, slug: str, *, triaged: str | None = None) -> Path:
    """Write a minimal gap stub, optionally with a triaged marker."""
    path = tmp_path / RESEARCHING_DIR / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = [f"id: {slug}", "type: gap", "status: discovered"]
    if triaged:
        fm_lines.append(f"triaged: {triaged}")
    fm = "\n".join(fm_lines)
    path.write_text(f"---\n{fm}\n---\n\n")
    return path


def _write_source_file(tmp_path: Path, note_id: str, body: str) -> Path:
    """Write a source note body to disk under _processed/."""
    path = tmp_path / "_processed" / f"{note_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# split_csv
# ---------------------------------------------------------------------------


class TestSplitCsv:
    """Full coverage of split_csv() including all documented edge cases."""

    def test_returns_none_for_none_input(self):
        assert split_csv(None) is None

    def test_returns_none_for_empty_string(self):
        assert split_csv("") is None

    def test_returns_none_for_whitespace_only(self):
        assert split_csv("   ") is None

    def test_returns_none_for_all_empty_segments(self):
        """Comma-separated string where every segment is blank -> None."""
        assert split_csv(",,, ,  ,") is None

    def test_single_value(self):
        assert split_csv("discovered") == ["discovered"]

    def test_multiple_values(self):
        assert split_csv("in_review,classified") == ["in_review", "classified"]

    def test_three_values(self):
        assert split_csv("external,internal,hybrid") == [
            "external",
            "internal",
            "hybrid",
        ]

    def test_strips_leading_and_trailing_whitespace_from_segments(self):
        assert split_csv(" in_review , classified ") == ["in_review", "classified"]

    def test_drops_empty_segments_between_commas(self):
        """Two commas adjacent produce an empty segment that is dropped."""
        assert split_csv("external,,internal") == ["external", "internal"]

    def test_strips_whitespace_and_drops_empty_together(self):
        assert split_csv(" external ,  , internal ") == ["external", "internal"]

    def test_single_value_with_surrounding_whitespace(self):
        assert split_csv("  parked  ") == ["parked"]

    def test_returns_list_not_none_when_at_least_one_value(self):
        """Explicit None-vs-list check: a non-empty result is a list, not None."""
        result = split_csv("x")
        assert result is not None
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# _rewrite_sources (direct unit tests)
# ---------------------------------------------------------------------------


class TestRewriteSources:
    """Direct unit tests for the _rewrite_sources internal helper.

    Exercises dry_run mode and error paths without going through the full
    discover_gaps -> Phase-A codepath.
    """

    def test_dry_run_does_not_write_file(self, session, tmp_path):
        """dry_run=True: count is returned but the file is not modified."""
        src = _make_note(session, "src", rel_path="_processed/src.md")
        body = "---\nid: src\ntype: atom\n---\n\nWe use [[Gap Concept]] here.\n"
        src_path = _write_source_file(tmp_path, "src", body)

        count = _rewrite_sources(
            session,
            tmp_path,
            "gap-concept",
            [src.note_id],
            dry_run=True,
        )

        assert count == 1
        # File must be unchanged.
        assert src_path.read_text() == body

    def test_live_run_writes_file(self, session, tmp_path):
        """dry_run=False: file is updated, count is returned."""
        src = _make_note(session, "src", rel_path="_processed/src.md")
        body = "---\nid: src\ntype: atom\n---\n\nWe use [[Gap Concept]] here.\n"
        src_path = _write_source_file(tmp_path, "src", body)

        count = _rewrite_sources(
            session,
            tmp_path,
            "gap-concept",
            [src.note_id],
            dry_run=False,
        )

        assert count == 1
        new_body = src_path.read_text()
        assert "[[Gap Concept]]" not in new_body
        assert "We use Gap Concept here." in new_body

    def test_returns_zero_when_no_matching_notes(self, session, tmp_path):
        """Requesting rewrite for note_ids that don't exist in DB -> 0."""
        count = _rewrite_sources(
            session,
            tmp_path,
            "gap-concept",
            ["nonexistent-note-id"],
            dry_run=False,
        )
        assert count == 0

    def test_returns_zero_when_no_wikilinks_in_body(self, session, tmp_path):
        """Body contains no [[Gap Concept]] link -> unlinkify_if_changed returns None
        -> count is not incremented."""
        src = _make_note(session, "src", rel_path="_processed/src.md")
        body = "---\nid: src\ntype: atom\n---\n\nNo wikilinks here at all.\n"
        _write_source_file(tmp_path, "src", body)

        count = _rewrite_sources(
            session,
            tmp_path,
            "gap-concept",
            [src.note_id],
            dry_run=False,
        )
        assert count == 0

    def test_oserror_on_read_is_skipped_and_logged(self, session, tmp_path, caplog):
        """FileNotFoundError while reading a source note is logged and skipped.
        The loop continues for other notes; the missing file is not counted.
        """
        src = _make_note(session, "src", rel_path="_processed/src.md")
        # Deliberately do NOT create the file on disk.

        with caplog.at_level(logging.WARNING, logger="knowledge.gaps"):
            count = _rewrite_sources(
                session,
                tmp_path,
                "gap-concept",
                [src.note_id],
                dry_run=False,
            )

        assert count == 0
        assert any("could not read" in r.getMessage() for r in caplog.records)

    def test_oserror_on_write_is_logged_and_not_counted(
        self, session, tmp_path, caplog
    ):
        """OSError on write: the note is NOT counted as rewritten."""
        src = _make_note(session, "src", rel_path="_processed/src.md")
        body = "---\nid: src\ntype: atom\n---\n\nWe use [[Gap Concept]] here.\n"
        _write_source_file(tmp_path, "src", body)

        real_write = Path.write_text

        def patched_write(self, data, *args, **kwargs):
            if "src.md" in str(self):
                raise OSError("simulated write error")
            return real_write(self, data, *args, **kwargs)

        with patch.object(Path, "write_text", patched_write):
            with caplog.at_level(logging.WARNING, logger="knowledge.gaps"):
                count = _rewrite_sources(
                    session,
                    tmp_path,
                    "gap-concept",
                    [src.note_id],
                    dry_run=False,
                )

        assert count == 0
        assert any("write failed" in r.getMessage() for r in caplog.records)

    def test_multiple_sources_partial_failure(self, session, tmp_path):
        """With two source notes, a read failure on one does not block the other."""
        src_ok = _make_note(session, "src-ok", rel_path="_processed/src-ok.md")
        src_bad = _make_note(session, "src-bad", rel_path="_processed/src-bad.md")
        # Only write the good source file.
        body = "---\nid: src-ok\ntype: atom\n---\n\nWe use [[Gap Concept]] here.\n"
        _write_source_file(tmp_path, "src-ok", body)
        # src-bad is absent on disk.

        count = _rewrite_sources(
            session,
            tmp_path,
            "gap-concept",
            [src_ok.note_id, src_bad.note_id],
            dry_run=False,
        )

        # Only the one successful rewrite counts.
        assert count == 1


# ---------------------------------------------------------------------------
# discover_gaps - Phase A: discardable-stub rewriting
# ---------------------------------------------------------------------------


class TestDiscoverGapsPhaseA:
    """Phase A: stub with triaged: discardable -> sources rewritten, write_stub
    skipped. Mirrors gap_discardable_rewrite_test.py which is not registered
    in the BUILD file."""

    def test_live_rewrite_when_flag_on(self, monkeypatch, session, tmp_path):
        """KNOWLEDGE_GAPS_REWRITE_DISCARDABLE=1 + discardable stub -> source
        notes are rewritten in-place; write_stub is NOT called for this slug."""
        monkeypatch.setenv("KNOWLEDGE_GAPS_REWRITE_DISCARDABLE", "1")
        src_body = (
            "---\nid: src\ntitle: Src\ntype: atom\n---\n\n"
            "We use [[Gone Concept]] often.\n"
        )
        src_path = _write_source_file(tmp_path, "src", src_body)
        stub_path = _write_stub(tmp_path, "gone-concept", triaged="discardable")
        src = _make_note(session, "src", rel_path="_processed/src.md", title="Src")
        _add_body_link(session, src_fk=src.id, target_id="gone-concept")

        discover_gaps(session, tmp_path)

        # Source body updated.
        rewritten = src_path.read_text()
        assert "[[Gone Concept]]" not in rewritten
        assert "We use Gone Concept often." in rewritten

        # write_stub was skipped for this slug — referenced_by never added.
        fm = yaml.safe_load(stub_path.read_text().split("---\n", 2)[1])
        assert "referenced_by" not in fm

    def test_dry_run_when_flag_off(self, monkeypatch, session, tmp_path):
        """Without the flag the source file is NOT modified."""
        monkeypatch.delenv("KNOWLEDGE_GAPS_REWRITE_DISCARDABLE", raising=False)
        body = (
            "---\nid: src\ntitle: Src\ntype: atom\n---\n\n"
            "We use [[Gone Concept]] often.\n"
        )
        src_path = _write_source_file(tmp_path, "src", body)
        _write_stub(tmp_path, "gone-concept", triaged="discardable")
        src = _make_note(session, "src", rel_path="_processed/src.md", title="Src")
        _add_body_link(session, src_fk=src.id, target_id="gone-concept")

        discover_gaps(session, tmp_path)

        assert src_path.read_text() == body

    def test_non_discardable_stub_normal_path(self, monkeypatch, session, tmp_path):
        """A stub without triaged: discardable proceeds through the normal
        write_stub path and creates a gap row."""
        monkeypatch.setenv("KNOWLEDGE_GAPS_REWRITE_DISCARDABLE", "1")
        src = _make_note(session, "src", title="Src")
        _add_body_link(session, src_fk=src.id, target_id="real-concept")

        count = discover_gaps(session, tmp_path)

        assert count == 1
        rows = (
            session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
        )
        assert len(rows) == 1
        assert rows[0].term == "real-concept"

    def test_stub_indexed_as_gap_note_still_fires_phase_a(
        self, monkeypatch, session, tmp_path
    ):
        """Regression: the reconciler indexes stubs as Note(type='gap'). The
        fix excludes type='gap' Notes from existing_note_ids so Phase A still
        sees those wikilinks as unresolved."""
        monkeypatch.setenv("KNOWLEDGE_GAPS_REWRITE_DISCARDABLE", "1")
        src_body = (
            "---\nid: src\ntitle: Src\ntype: atom\n---\n\nWe use [[Throwaway]] often.\n"
        )
        src_path = _write_source_file(tmp_path, "src", src_body)
        _write_stub(tmp_path, "throwaway", triaged="discardable")
        src = _make_note(session, "src", rel_path="_processed/src.md", title="Src")
        # Simulate reconciler indexing the stub as a type='gap' Note.
        gap_note = Note(
            note_id="throwaway",
            path=f"{RESEARCHING_DIR}/throwaway.md",
            title="throwaway",
            content_hash="stub-throwaway",
            type="gap",
        )
        session.add(gap_note)
        session.commit()
        _add_body_link(session, src_fk=src.id, target_id="throwaway")

        discover_gaps(session, tmp_path)

        rewritten = src_path.read_text()
        assert "[[Throwaway]]" not in rewritten
        assert "We use Throwaway often." in rewritten


# ---------------------------------------------------------------------------
# discover_gaps - Phase B: tombstoning
# ---------------------------------------------------------------------------


class TestDiscoverGapsPhaseB:
    """Phase B: orphan discardable gaps (no remaining source refs) are deleted."""

    def test_tombstones_gap_and_stub_when_refs_gone(
        self, monkeypatch, session, tmp_path
    ):
        """Gap row + discardable stub with zero source refs -> both deleted."""
        monkeypatch.setenv("KNOWLEDGE_GAPS_REWRITE_DISCARDABLE", "1")
        session.add(
            Gap(
                term="gone-term",
                note_id="gone-term",
                pipeline_version=GAPS_PIPELINE_VERSION,
                state="discovered",
            )
        )
        session.commit()
        stub_path = _write_stub(tmp_path, "gone-term", triaged="discardable")

        discover_gaps(session, tmp_path)

        rows = (
            session.execute(
                select(Gap).where(Gap.note_id == "gone-term", Gap.deleted_at.is_(None))
            )
            .scalars()
            .all()
        )
        assert rows == []
        assert not stub_path.exists()

    def test_does_not_tombstone_keep_marked_stub(self, monkeypatch, session, tmp_path):
        """triaged: keep -> preserved even with no source refs."""
        monkeypatch.setenv("KNOWLEDGE_GAPS_REWRITE_DISCARDABLE", "1")
        session.add(
            Gap(
                term="kept-term",
                note_id="kept-term",
                pipeline_version=GAPS_PIPELINE_VERSION,
                state="discovered",
            )
        )
        session.commit()
        stub_path = _write_stub(tmp_path, "kept-term", triaged="keep")

        discover_gaps(session, tmp_path)

        rows = (
            session.execute(
                select(Gap).where(Gap.note_id == "kept-term", Gap.deleted_at.is_(None))
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert stub_path.exists()

    def test_does_not_tombstone_unmarked_stub(self, monkeypatch, session, tmp_path):
        """A stub without any triage marker is preserved."""
        monkeypatch.setenv("KNOWLEDGE_GAPS_REWRITE_DISCARDABLE", "1")
        session.add(
            Gap(
                term="unmarked-term",
                note_id="unmarked-term",
                pipeline_version=GAPS_PIPELINE_VERSION,
                state="discovered",
            )
        )
        session.commit()
        stub_path = _write_stub(tmp_path, "unmarked-term", triaged=None)

        discover_gaps(session, tmp_path)

        rows = (
            session.execute(
                select(Gap).where(
                    Gap.note_id == "unmarked-term", Gap.deleted_at.is_(None)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert stub_path.exists()

    def test_does_not_tombstone_when_missing_stub_file(
        self, monkeypatch, session, tmp_path
    ):
        """Orphan Gap row with no stub file: is_discardable returns False, row
        is preserved (data drift case)."""
        monkeypatch.setenv("KNOWLEDGE_GAPS_REWRITE_DISCARDABLE", "1")
        session.add(
            Gap(
                term="ghost",
                note_id="ghost",
                pipeline_version=GAPS_PIPELINE_VERSION,
                state="discovered",
            )
        )
        session.commit()
        # No stub written.

        discover_gaps(session, tmp_path)

        rows = (
            session.execute(
                select(Gap).where(Gap.note_id == "ghost", Gap.deleted_at.is_(None))
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    def test_does_not_tombstone_when_refs_still_present(
        self, monkeypatch, session, tmp_path
    ):
        """Even with triaged: discardable, an active source ref prevents tombstoning
        in the same cycle (Phase A rewrites first; tombstone waits for next cycle)."""
        monkeypatch.setenv("KNOWLEDGE_GAPS_REWRITE_DISCARDABLE", "1")
        session.add(
            Gap(
                term="active-discard",
                note_id="active-discard",
                pipeline_version=GAPS_PIPELINE_VERSION,
                state="discovered",
            )
        )
        session.commit()
        stub_path = _write_stub(tmp_path, "active-discard", triaged="discardable")
        src_body = (
            "---\nid: src\ntitle: Src\ntype: atom\n---\n\n"
            "We use [[Active Discard]] often.\n"
        )
        _write_source_file(tmp_path, "src", src_body)
        src = _make_note(session, "src", rel_path="_processed/src.md", title="Src")
        _add_body_link(session, src_fk=src.id, target_id="active-discard")

        discover_gaps(session, tmp_path)

        # Gap row and stub still exist.
        rows = (
            session.execute(
                select(Gap).where(
                    Gap.note_id == "active-discard", Gap.deleted_at.is_(None)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert stub_path.exists()


# ---------------------------------------------------------------------------
# discover_gaps - slug folding edge cases
# ---------------------------------------------------------------------------


class TestDiscoverGapsSlugFolding:
    """Two distinct terms that hash to the same slug collapse into one Gap row."""

    def test_two_terms_same_slug_produce_one_gap(self, session, tmp_path):
        """e.g. 'Foo Bar' and 'foo-bar' both slug to 'foo-bar'."""
        src_a = _make_note(session, "src-a", title="Source A")
        src_b = _make_note(session, "src-b", title="Source B")
        # Both terms slugify to "outside-in-tdd".
        _add_body_link(session, src_fk=src_a.id, target_id="Outside-In TDD")
        _add_body_link(session, src_fk=src_b.id, target_id="Outside In TDD")

        discover_gaps(session, tmp_path)

        rows = (
            session.execute(
                select(Gap).where(
                    Gap.note_id == "outside-in-tdd", Gap.deleted_at.is_(None)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, (
            f"Expected 1 Gap, got {len(rows)}: {[r.term for r in rows]}"
        )

    def test_gap_stubs_excluded_from_resolved_note_ids(self, session, tmp_path):
        """Notes of type='gap' (stubs) are excluded from existing_note_ids so
        wikilinks that point at a stub slug are still seen as unresolved."""
        # Index a stub as a type='gap' Note.
        stub_note = Note(
            note_id="stub-slug",
            path=f"{RESEARCHING_DIR}/stub-slug.md",
            title="stub-slug",
            content_hash="stub-hash",
            type="gap",
        )
        session.add(stub_note)
        session.commit()

        src = _make_note(session, "src", title="Src")
        _add_body_link(session, src_fk=src.id, target_id="stub-slug")

        count = discover_gaps(session, tmp_path)

        # The wikilink pointing at the gap stub is NOT resolved — a new Gap
        # row must be inserted.
        assert count == 1
        rows = (
            session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# classify_gaps - edge cases
# ---------------------------------------------------------------------------


class TestClassifyGapsEdgeCases:
    """Edge cases not covered by gap_lifecycle_test.py."""

    def test_none_classifier_with_no_pending_gaps_returns_zero_without_warning(
        self, session, caplog
    ):
        """When no discovered gaps exist and classifier is None, classify_gaps
        must return 0 and emit no warning (the warning is conditional on
        pending > 0)."""
        with caplog.at_level(logging.WARNING, logger="knowledge.gaps"):
            result = classify_gaps(session, classifier=None)

        assert result == 0
        assert not any(
            "gaps awaiting classification" in r.getMessage() for r in caplog.records
        )

    def test_none_classifier_with_pending_gaps_logs_warning(
        self, session, tmp_path, caplog
    ):
        """With pending gaps and no classifier, exactly one warning is logged."""
        src = _make_note(session, "s", title="S")
        _add_body_link(session, src_fk=src.id, target_id="pending-term")
        discover_gaps(session, tmp_path)

        with caplog.at_level(logging.WARNING, logger="knowledge.gaps"):
            result = classify_gaps(session, classifier=None)

        assert result == 0
        assert any(
            "gaps awaiting classification" in r.getMessage() for r in caplog.records
        )

    def test_invalid_classifier_output_falls_back_to_internal(
        self, session, tmp_path, caplog
    ):
        """Privacy-conservative fallback: bogus classifier output -> internal."""
        src = _make_note(session, "s", title="S")
        _add_body_link(session, src_fk=src.id, target_id="mystery")
        discover_gaps(session, tmp_path)

        def classifier(term: str, _ctx: str) -> str:
            return "bogus-class"

        with caplog.at_level(logging.WARNING, logger="knowledge.gaps"):
            count = classify_gaps(session, classifier=classifier)

        assert count == 1
        gap = session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalar_one()
        assert gap.gap_class == "internal"
        assert gap.state == "in_review"

    def test_classify_sets_classified_at_timestamp(self, session, tmp_path):
        """classified_at must be set on every classified gap."""
        src = _make_note(session, "s", title="S")
        _add_body_link(session, src_fk=src.id, target_id="term")
        discover_gaps(session, tmp_path)

        classify_gaps(session, classifier=lambda t, c: "external")

        gap = session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalar_one()
        assert gap.classified_at is not None


# ---------------------------------------------------------------------------
# list_review_queue - edge cases
# ---------------------------------------------------------------------------


class TestListReviewQueueEdgeCases:
    """Return-shape and filtering guarantees for list_review_queue."""

    def test_result_dicts_contain_required_keys(self, session):
        """Each element must contain at least id, term, context, gap_class, created_at.

        Extra keys (state, resolved_at, human_verified) are permitted; the
        review UI consumes them in audit mode. Use subset semantics so
        adding response fields later doesn't break this assertion.
        """
        gap = Gap(
            term="test-term",
            context="some context",
            gap_class="internal",
            state="in_review",
            pipeline_version=GAPS_PIPELINE_VERSION,
        )
        session.add(gap)
        session.commit()

        queue = list_review_queue(session)

        assert len(queue) == 1
        item = queue[0]
        required = {"id", "term", "context", "gap_class", "created_at"}
        assert required.issubset(item.keys()), (
            f"missing required keys: {required - item.keys()}"
        )
        assert item["term"] == "test-term"
        assert item["context"] == "some context"
        assert item["gap_class"] == "internal"

    def test_excludes_external_in_review_gaps(self, session):
        """state=in_review but gap_class=external must NOT appear in the queue."""
        gap = Gap(
            term="ext",
            context="",
            gap_class="external",
            state="in_review",
            pipeline_version=GAPS_PIPELINE_VERSION,
        )
        session.add(gap)
        session.commit()

        assert list_review_queue(session) == []


# ---------------------------------------------------------------------------
# answer_gap - collision suffix escalation
# ---------------------------------------------------------------------------


class TestAnswerGapCollisionSuffix:
    """Multi-level filename collision resolution (-1, -2, ...)."""

    def _seed_gap(self, session: Session, term: str) -> int:
        gap = Gap(
            term=term,
            context="",
            gap_class="internal",
            state="in_review",
            pipeline_version=GAPS_PIPELINE_VERSION,
        )
        session.add(gap)
        session.commit()
        session.refresh(gap)
        return gap.id

    def test_second_collision_gets_minus_two_suffix(self, session, tmp_path):
        """When both slug.md and slug-1.md exist, answer_gap must use slug-2.md."""
        gap_id = self._seed_gap(session, "Multi Level")

        processed = tmp_path / "_processed"
        processed.mkdir(parents=True)
        (processed / "multi-level.md").write_text("first")
        (processed / "multi-level-1.md").write_text("second")

        result = answer_gap(session, gap_id, "the answer", tmp_path)

        assert result["note_id"] == "multi-level-2"
        assert result["path"] == "_processed/multi-level-2.md"
        assert (processed / "multi-level-2.md").is_file()
        # Original files untouched.
        assert (processed / "multi-level.md").read_text() == "first"
        assert (processed / "multi-level-1.md").read_text() == "second"

    def test_no_collision_uses_bare_slug(self, session, tmp_path):
        """Happy path: no pre-existing file -> bare slug.md used."""
        gap_id = self._seed_gap(session, "Fresh Term")

        result = answer_gap(session, gap_id, "some answer", tmp_path)

        assert result["note_id"] == "fresh-term"
        assert result["path"] == "_processed/fresh-term.md"

    def test_frontmatter_id_matches_filename_stem_after_collision(
        self, session, tmp_path
    ):
        """The ``id`` in frontmatter must equal the collision-resolved stem."""
        gap_id = self._seed_gap(session, "Clash")

        processed = tmp_path / "_processed"
        processed.mkdir(parents=True)
        (processed / "clash.md").write_text("existing")

        result = answer_gap(session, gap_id, "content", tmp_path)

        file_path = tmp_path / result["path"]
        _, fm_block, _ = file_path.read_text().split("---\n", 2)
        fm = yaml.safe_load(fm_block)
        # id in frontmatter must match the collision-resolved note_id.
        assert fm["id"] == result["note_id"]  # "clash-1", not "clash"
