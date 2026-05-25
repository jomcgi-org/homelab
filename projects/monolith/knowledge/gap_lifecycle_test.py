"""Unit tests for knowledge.gaps — discover/classify/review/answer lifecycle.

Uses the same in-memory SQLite + schema-strip fixture pattern as
``gap_model_test.py`` so table DDL works without a real Postgres.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.gap_stubs import RESEARCHING_DIR, parse_stub_frontmatter, write_stub
from knowledge.gaps import (
    GAPS_PIPELINE_VERSION,
    answer_gap,
    classify_gaps,
    discover_gaps,
    list_review_queue,
)
from knowledge.models import Gap, Note, NoteLink


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


def _make_note(
    session: Session,
    note_id: str,
    *,
    title: str | None = None,
) -> Note:
    note = Note(
        note_id=note_id,
        path=f"_processed/{note_id}.md",
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


# ---------------------------------------------------------------------------
# discover_gaps
# ---------------------------------------------------------------------------


def test_discover_gaps_finds_unresolved_wikilink(session, tmp_path):
    src = _make_note(session, "source-note", title="Source Note")
    _add_body_link(session, src_fk=src.id, target_id="missing-concept")

    created = discover_gaps(session, tmp_path)

    assert created == 1
    gaps = session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.term == "missing-concept"
    assert gap.context == "Source Note"
    assert gap.state == "discovered"
    assert gap.gap_class is None
    assert gap.pipeline_version == GAPS_PIPELINE_VERSION
    # The stub-notes extension also sets note_id (= slug) and writes a stub.
    assert gap.note_id == "missing-concept"
    assert (tmp_path / RESEARCHING_DIR / "missing-concept.md").is_file()


def test_discover_gaps_skips_resolved_links(session, tmp_path):
    src = _make_note(session, "source-note", title="Source")
    _make_note(session, "target-note", title="Target")  # link target exists
    _add_body_link(session, src_fk=src.id, target_id="target-note")

    created = discover_gaps(session, tmp_path)

    assert created == 0
    assert (
        session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
        == []
    )
    # No stub should have been written.
    assert not (tmp_path / RESEARCHING_DIR).exists() or not list(
        (tmp_path / RESEARCHING_DIR).iterdir()
    )


def test_discover_gaps_skips_links_resolved_via_alias(session, tmp_path):
    """Wikilinks slugged to a canonical atom's alias are not gaps.

    Pre-fix, only ``note_id`` was checked — so ``[[Bayes' Theorem]]``
    slugified to ``bayes-theorem`` would be a false-positive gap when the
    canonical atom lives at a different slug with "Bayes' Theorem" in
    ``aliases:``. Post-fix, slugified aliases participate in the coverage
    check.
    """
    src = _make_note(session, "source-note", title="Source")
    # Canonical lives at a different slug than the wikilink's slugified target.
    target = Note(
        note_id="probability-update-rule",
        path="_processed/probability-update-rule.md",
        title="Probability Update Rule",
        content_hash="hash-probability-update-rule",
        type="atom",
        aliases=["Bayes' Theorem", "Bayes' Rule"],
    )
    session.add(target)
    session.commit()

    # The wikilink extractor would produce this slug from body text
    # ``[[Bayes' Theorem]]``; absent alias-awareness this would be a gap.
    _add_body_link(session, src_fk=src.id, target_id="bayes-theorem")

    created = discover_gaps(session, tmp_path)

    assert created == 0
    assert (
        session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
        == []
    )
    assert not (tmp_path / RESEARCHING_DIR).exists() or not list(
        (tmp_path / RESEARCHING_DIR).iterdir()
    )


def test_discover_gaps_is_idempotent(session, tmp_path):
    src = _make_note(session, "src", title="Src")
    _add_body_link(session, src_fk=src.id, target_id="missing")

    first = discover_gaps(session, tmp_path)
    stub_path = tmp_path / RESEARCHING_DIR / "missing.md"
    first_stub = stub_path.read_text()
    second = discover_gaps(session, tmp_path)

    assert first == 1
    assert second == 0
    assert (
        len(
            session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
        )
        == 1
    )
    # Stub still exists and is unchanged on re-run (write_stub is idempotent).
    assert stub_path.read_text() == first_stub


def test_discover_gaps_ignores_frontmatter_edges(session, tmp_path):
    """kind='edge' rows are typed assertions, not wikilink gaps."""
    src = _make_note(session, "src", title="Src")
    session.add(
        NoteLink(
            src_note_fk=src.id,
            target_id="derived-target",
            target_title=None,
            kind="edge",
            edge_type="derives_from",
        )
    )
    session.commit()

    assert discover_gaps(session, tmp_path) == 0


def test_discover_gaps_captures_source_title_as_context(session, tmp_path):
    src = _make_note(session, "src-slug", title="Kubernetes Networking")
    _add_body_link(session, src_fk=src.id, target_id="cilium")

    discover_gaps(session, tmp_path)

    gap = session.execute(
        select(Gap).where(Gap.term == "cilium", Gap.deleted_at.is_(None))
    ).scalar_one()
    assert gap.context == "Kubernetes Networking"


def test_discover_gaps_writes_stub_file(session, tmp_path):
    """discover_gaps sets note_id on the Gap row and writes a stub to _researching/."""
    src = _make_note(session, "source-note", title="Source Note")
    _add_body_link(session, src_fk=src.id, target_id="some-term")

    assert discover_gaps(session, tmp_path) == 1

    gap = session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalar_one()
    assert gap.term == "some-term"
    assert gap.note_id == "some-term"

    stub = tmp_path / RESEARCHING_DIR / "some-term.md"
    assert stub.is_file()
    meta = parse_stub_frontmatter(stub)
    assert meta["id"] == "some-term"
    assert meta["title"] == "some-term"
    assert meta["type"] == "gap"
    assert meta["status"] == "discovered"
    assert meta["referenced_by"] == ["source-note"]


def test_discover_gaps_heals_missing_stub(session, tmp_path):
    """If a Gap row exists without a stub, a subsequent run writes the stub."""
    src = _make_note(session, "source-note", title="Source Note")
    # Seed a Gap row directly without note_id and without a stub file.
    gap = Gap(
        term="orphan",
        context="Source Note",
        note_id=None,
        pipeline_version=GAPS_PIPELINE_VERSION,
        state="discovered",
    )
    session.add(gap)
    session.commit()
    _add_body_link(session, src_fk=src.id, target_id="orphan")

    # Row exists but stub is missing → the run writes the stub and backfills
    # note_id. The OR-collapse in discover_gaps means stub repair alone
    # counts as exactly one unit of work — not two (row + stub).
    created = discover_gaps(session, tmp_path)
    assert created == 1

    session.refresh(gap)
    assert gap.note_id == "orphan"

    stub = tmp_path / RESEARCHING_DIR / "orphan.md"
    assert stub.is_file()


def test_discover_gaps_backfills_existing_rows_with_no_stubs(session, tmp_path):
    """Simulates the post-migration scenario: Gap rows exist with note_id=NULL
    and no stub files. discover_gaps should set note_id on each row AND write
    matching stubs on the next run.

    Covers the 1793-row backfill that runs on first gardener cycle after the
    Task 1 migration lands in prod.
    """
    # Seed a source note that all the orphaned Gap rows will reference.
    source = _make_note(session, "source", title="Source Note")

    # Seed 5 Gap rows directly — simulating the post-migration state:
    # note_id=NULL and no stub files. These rows exist from PR #2193 before
    # the Task 1 migration added the note_id column.
    terms = ["foo", "bar", "baz", "qux", "zap"]
    session.add_all(
        [
            Gap(
                term=term,
                context="Source Note",
                pipeline_version=GAPS_PIPELINE_VERSION,
                state="discovered",
            )
            for term in terms
        ]
    )
    session.commit()

    # Also seed a NoteLink pointing at each term, so discover_gaps can see the
    # wikilinks and trigger the heal path.
    for term in terms:
        _add_body_link(session, src_fk=source.id, target_id=term)

    # Pre-conditions: no note_id, no stubs
    gaps_before = (
        session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
    )
    assert all(g.note_id is None for g in gaps_before)
    for term in terms:
        stub = tmp_path / RESEARCHING_DIR / f"{term}.md"
        assert not stub.exists()

    # Run discover_gaps — this is what the gardener's next cycle would do
    discover_gaps(session, tmp_path)

    # Post-conditions: each Gap row has note_id set, each stub exists
    gaps_after = (
        session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
    )
    assert len(gaps_after) == len(terms), "no duplicate rows inserted"
    for gap in gaps_after:
        assert gap.note_id is not None, f"note_id still NULL for {gap.term}"
        assert gap.note_id == gap.term, f"slug mismatch: {gap.note_id} vs {gap.term}"
        stub = tmp_path / RESEARCHING_DIR / f"{gap.note_id}.md"
        assert stub.is_file(), f"stub missing for {gap.term}"


def test_discover_gaps_heals_missing_row(session, tmp_path):
    """If a stub exists without a Gap row (edge case), discover_gaps inserts the row
    WITHOUT overwriting the stub — classifier edits survive re-discovery."""
    # Write a stub that has a classifier edit already applied.
    # The design rests on this edit surviving re-runs of discover_gaps.
    stub = tmp_path / RESEARCHING_DIR / "orphan.md"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "---\n"
        "id: orphan\n"
        "title: orphan\n"
        "type: gap\n"
        "status: classified\n"
        "gap_class: external\n"
        "referenced_by:\n"
        "  - note-a\n"
        'discovered_at: "2026-04-25T08:00:00Z"\n'
        'classified_at: "2026-04-25T08:05:00Z"\n'
        'classifier_version: "opus-4-7@v1"\n'
        "---\n\n"
    )
    stub_bytes_before = stub.read_bytes()

    # Seed a source note + NoteLink pointing at 'orphan' — simulates a vault
    # state where the stub pre-exists but no Gap row has been inserted yet
    # (e.g., post-migration backfill of an orphan stub).
    src = _make_note(session, "note-a", title="Note A")
    _add_body_link(session, src_fk=src.id, target_id="orphan")

    created = discover_gaps(session, tmp_path)

    # The row got inserted (new work).
    assert created == 1

    # Critically: the stub's bytes are unchanged. Classifier edits survive.
    assert stub.read_bytes() == stub_bytes_before

    # The Gap row reflects the stub's existence via note_id.
    gap = session.execute(
        select(Gap).where(Gap.term == "orphan", Gap.deleted_at.is_(None))
    ).scalar_one()
    assert gap.note_id == "orphan"


def test_discover_gaps_dedupes_referenced_by(session, tmp_path):
    """Two source notes referencing the same term produce ONE Gap row and ONE stub,
    with referenced_by listing both source notes sorted for determinism."""
    src_a = _make_note(session, "note-a", title="Note A")
    src_b = _make_note(session, "note-b", title="Note B")
    _add_body_link(session, src_fk=src_a.id, target_id="shared-term")
    _add_body_link(session, src_fk=src_b.id, target_id="shared-term")

    assert discover_gaps(session, tmp_path) == 1

    gaps = session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
    assert len(gaps) == 1
    assert gaps[0].term == "shared-term"
    assert gaps[0].note_id == "shared-term"

    researching = tmp_path / RESEARCHING_DIR
    stubs = list(researching.iterdir())
    assert len(stubs) == 1
    assert stubs[0].name == "shared-term.md"

    meta = parse_stub_frontmatter(stubs[0])
    assert meta["referenced_by"] == ["note-a", "note-b"]


def test_discover_gaps_collapses_slug_collisions(session, tmp_path):
    """Two terms slugging to the same note_id collapse into one Gap row.

    Without this fold, the loop iterates once per distinct term and the second
    INSERT trips the UNIQUE(note_id) constraint added in Task 1 — the
    SAVEPOINT swallows the IntegrityError and the second term's
    referenced_by source is silently lost.
    """
    src_a = _make_note(session, "src-a", title="Source A")
    src_b = _make_note(session, "src-b", title="Source B")
    # Two distinct terms that both slug to "outside-in-tdd".
    _add_body_link(session, src_fk=src_a.id, target_id="Outside-In TDD")
    _add_body_link(session, src_fk=src_b.id, target_id="Outside In TDD")

    discover_gaps(session, tmp_path)

    rows = (
        session.execute(
            select(Gap).where(Gap.note_id == "outside-in-tdd", Gap.deleted_at.is_(None))
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, (
        f"Expected one Gap per note_id, got {len(rows)}: {[r.term for r in rows]}"
    )

    stub_path = tmp_path / RESEARCHING_DIR / "outside-in-tdd.md"
    assert stub_path.is_file()
    meta = parse_stub_frontmatter(stub_path)
    assert sorted(meta["referenced_by"]) == ["src-a", "src-b"], (
        f"Expected union of both source notes; got {meta['referenced_by']}"
    )


# ---------------------------------------------------------------------------
# classify_gaps
# ---------------------------------------------------------------------------


def test_classify_gaps_without_classifier_is_noop(session, tmp_path, caplog):
    """No classifier wired → gaps stay at discovered, warning logged.

    Routing unclassified gaps to internal would conflate classifier absence
    with classifier uncertainty. The review queue must only populate once
    a real classifier lands (Task 3).
    """
    src = _make_note(session, "s", title="S")
    _add_body_link(session, src_fk=src.id, target_id="t1")
    _add_body_link(session, src_fk=src.id, target_id="t2")
    discover_gaps(session, tmp_path)

    with caplog.at_level(logging.WARNING, logger="knowledge.gaps"):
        classified = classify_gaps(session)  # no classifier wired

    assert classified == 0
    for gap in (
        session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
    ):
        assert gap.gap_class is None
        assert gap.state == "discovered"
        assert gap.classified_at is None

    assert any(
        "2 gaps awaiting classification but no classifier is wired"
        in record.getMessage()
        for record in caplog.records
    )


def test_classify_gaps_routes_by_class(session, tmp_path):
    src = _make_note(session, "s", title="S")
    for target in ("ext", "int", "hyb", "park"):
        _add_body_link(session, src_fk=src.id, target_id=target)
    discover_gaps(session, tmp_path)

    mapping = {
        "ext": "external",
        "int": "internal",
        "hyb": "hybrid",
        "park": "parked",
    }

    def classifier(term: str, _context: str) -> str:
        return mapping[term]

    assert classify_gaps(session, classifier=classifier) == 4

    rows = {
        g.term: (g.gap_class, g.state)
        for g in session.execute(select(Gap).where(Gap.deleted_at.is_(None)))
        .scalars()
        .all()
    }
    assert rows["ext"] == ("external", "classified")
    assert rows["int"] == ("internal", "in_review")
    assert rows["hyb"] == ("hybrid", "in_review")
    assert rows["park"] == ("parked", "classified")


def test_classify_gaps_skips_already_classified(session, tmp_path):
    src = _make_note(session, "s", title="S")
    _add_body_link(session, src_fk=src.id, target_id="x")
    discover_gaps(session, tmp_path)

    def classifier(_term: str, _context: str) -> str:
        return "internal"

    assert classify_gaps(session, classifier=classifier) == 1
    # Second call finds nothing in state='discovered'.
    assert classify_gaps(session, classifier=classifier) == 0


# ---------------------------------------------------------------------------
# list_review_queue
# ---------------------------------------------------------------------------


def test_list_review_queue_only_returns_internal_hybrid_in_review(session):
    src = _make_note(session, "s", title="S")
    # Manually construct gaps in varied states so we can assert filtering.
    now = datetime.now(timezone.utc)
    gaps = [
        Gap(
            term="a-internal",
            context="",
            gap_class="internal",
            state="in_review",
            pipeline_version=GAPS_PIPELINE_VERSION,
            created_at=now - timedelta(seconds=30),
        ),
        Gap(
            term="b-hybrid",
            context="",
            gap_class="hybrid",
            state="in_review",
            pipeline_version=GAPS_PIPELINE_VERSION,
            created_at=now - timedelta(seconds=20),
        ),
        Gap(
            term="c-external",
            context="",
            gap_class="external",
            state="classified",
            pipeline_version=GAPS_PIPELINE_VERSION,
            created_at=now - timedelta(seconds=10),
        ),
        Gap(
            term="d-internal-discovered",
            context="",
            gap_class="internal",
            state="discovered",
            pipeline_version=GAPS_PIPELINE_VERSION,
            created_at=now,
        ),
    ]
    session.add_all(gaps)
    session.commit()

    queue = list_review_queue(session)

    terms = [row["term"] for row in queue]
    assert terms == ["a-internal", "b-hybrid"]  # FIFO, only in_review+internal/hybrid
    assert queue[0]["gap_class"] == "internal"
    assert queue[1]["gap_class"] == "hybrid"


def test_list_review_queue_empty(session):
    assert list_review_queue(session) == []


# ---------------------------------------------------------------------------
# answer_gap
# ---------------------------------------------------------------------------


def _seed_reviewable_gap(session: Session, *, term: str = "Linkerd mTLS") -> int:
    src = _make_note(session, "src", title="Src")
    gap = Gap(
        term=term,
        context="networking note",
        gap_class="internal",
        state="in_review",
        pipeline_version=GAPS_PIPELINE_VERSION,
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)
    return gap.id


def test_answer_gap_writes_file_and_commits(session, tmp_path):
    gap_id = _seed_reviewable_gap(session, term="Linkerd mTLS")

    result = answer_gap(
        session,
        gap_id,
        "Linkerd enables mTLS via per-pod sidecar proxies on port 4143.",
        tmp_path,
    )

    assert result["gap_id"] == gap_id
    assert result["note_id"] == "linkerd-mtls"
    assert result["path"] == "_processed/linkerd-mtls.md"

    file_path = tmp_path / "_processed" / "linkerd-mtls.md"
    assert file_path.is_file()
    content = file_path.read_text()
    assert content.startswith("---\n")
    # Parse the frontmatter so we don't couple to yaml.dump's quoting style.
    _, fm_block, body = content.split("---\n", 2)
    fm = yaml.safe_load(fm_block)
    assert fm == {
        "id": "linkerd-mtls",
        "title": "Linkerd mTLS",
        "type": "atom",
        "source_tier": "personal",
    }
    assert "Linkerd enables mTLS" in body

    gap = session.get(Gap, gap_id)
    assert gap.state == "committed"
    assert gap.answer.startswith("Linkerd enables mTLS")
    assert gap.resolved_at is not None


def test_answer_gap_handles_filename_collisions(session, tmp_path):
    gap_id = _seed_reviewable_gap(session, term="Collision")

    # Pre-create the unadorned file to force a -1 suffix.
    processed = tmp_path / "_processed"
    processed.mkdir(parents=True)
    (processed / "collision.md").write_text("existing")

    result = answer_gap(session, gap_id, "the answer", tmp_path)

    assert result["note_id"] == "collision-1"
    assert result["path"] == "_processed/collision-1.md"
    assert (processed / "collision-1.md").is_file()
    # Original file is untouched.
    assert (processed / "collision.md").read_text() == "existing"


def test_answer_gap_rejects_unknown_id(session, tmp_path):
    with pytest.raises(ValueError, match="Gap not found"):
        answer_gap(session, gap_id=9999, answer="x", vault_root=tmp_path)


def test_answer_gap_rejects_wrong_state(session, tmp_path):
    src = _make_note(session, "src", title="Src")
    gap = Gap(
        term="still-discovered",
        context="",
        state="discovered",
        pipeline_version=GAPS_PIPELINE_VERSION,
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)

    with pytest.raises(ValueError, match="expected 'in_review'"):
        answer_gap(session, gap.id, "x", tmp_path)


def test_answer_gap_handles_special_chars_in_term(session, tmp_path):
    """YAML frontmatter must round-trip arbitrary terms (C1 regression)."""
    term = r'regex \d+ "quoted" stuff'
    gap_id = _seed_reviewable_gap(session, term=term)

    result = answer_gap(session, gap_id, "some answer body", tmp_path)

    file_path = tmp_path / result["path"]
    content = file_path.read_text()
    # Split on the frontmatter fences and parse with a real YAML loader —
    # this would fail against the old hand-rolled escape, which emitted
    # title: "regex \d+ ..." (invalid escape sequence in double-quoted YAML).
    _, fm_block, _ = content.split("---\n", 2)
    fm = yaml.safe_load(fm_block)
    assert fm["title"] == term


def test_classify_gaps_rejects_invalid_classifier_output(session, tmp_path, caplog):
    """Out-of-range classifier outputs fall back to internal (I2 regression)."""
    src = _make_note(session, "s", title="S")
    _add_body_link(session, src_fk=src.id, target_id="good")
    _add_body_link(session, src_fk=src.id, target_id="bad")
    discover_gaps(session, tmp_path)

    def classifier(term: str, _context: str) -> str:
        return "external" if term == "good" else "bogus"

    with caplog.at_level(logging.WARNING, logger="knowledge.gaps"):
        assert classify_gaps(session, classifier=classifier) == 2

    rows = {
        g.term: (g.gap_class, g.state)
        for g in session.execute(select(Gap).where(Gap.deleted_at.is_(None)))
        .scalars()
        .all()
    }
    assert rows["good"] == ("external", "classified")
    assert rows["bad"] == ("internal", "in_review")
    assert any(
        "classifier returned invalid class 'bogus'" in record.getMessage()
        for record in caplog.records
    )


def test_answer_gap_rejects_frontmatter_terminator_in_answer(session, tmp_path):
    """Answers containing '---' on their own line must be rejected (M4)."""
    gap_id = _seed_reviewable_gap(session, term="some-term")

    with pytest.raises(ValueError, match="frontmatter terminator"):
        answer_gap(session, gap_id, "foo\n---\nbar", tmp_path)

    gap = session.get(Gap, gap_id)
    assert gap.state == "in_review"
    assert gap.answer is None
    assert gap.resolved_at is None


def test_answer_gap_deletes_stub_on_commit(session, tmp_path):
    """After answer_gap, the stub at _researching/<slug>.md is gone; the
    atom at _processed/<slug>.md exists."""
    note = _make_note(session, "source", title="Source")
    gap = Gap(
        term="linkerd-mtls",
        note_id="linkerd-mtls",
        state="in_review",
        gap_class="internal",
        pipeline_version=GAPS_PIPELINE_VERSION,
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)

    write_stub(
        vault_root=tmp_path,
        note_id="linkerd-mtls",
        title="linkerd-mtls",
        referenced_by=["source"],
        discovered_at="2026-04-25T08:00:00Z",
    )
    stub_path = tmp_path / RESEARCHING_DIR / "linkerd-mtls.md"
    assert stub_path.is_file()  # precondition

    result = answer_gap(
        session=session,
        gap_id=gap.id,
        answer="mTLS handles mutual authentication.",
        vault_root=tmp_path,
    )

    # Atom was created at _processed/
    atom_path = tmp_path / "_processed" / "linkerd-mtls.md"
    assert atom_path.is_file()
    # Stub was removed
    assert not stub_path.exists()
    # Return value includes path info
    assert result["gap_id"] == gap.id
    assert result["note_id"] == "linkerd-mtls"


def test_answer_gap_succeeds_when_stub_missing(session, tmp_path):
    """If the stub was deleted out-of-band (user hand-deleted or never
    existed), answer_gap still succeeds — the atom write is the authoritative
    source of truth."""
    note = _make_note(session, "source", title="Source")
    gap = Gap(
        term="floating-gap",
        note_id="floating-gap",
        state="in_review",
        gap_class="internal",
        pipeline_version=GAPS_PIPELINE_VERSION,
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)

    # No stub is written — _researching/ doesn't even exist

    result = answer_gap(
        session=session,
        gap_id=gap.id,
        answer="some answer",
        vault_root=tmp_path,
    )

    # Atom was created
    atom_path = tmp_path / "_processed" / "floating-gap.md"
    assert atom_path.is_file()
    # No crash — function returned normally
    assert result["note_id"] == "floating-gap"

    # Gap is committed
    session.refresh(gap)
    assert gap.state == "committed"
