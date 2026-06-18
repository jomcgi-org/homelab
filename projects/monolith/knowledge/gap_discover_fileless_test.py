"""Unit tests for the fileless :func:`knowledge.gaps.discover_gaps`.

``discover_gaps`` is Postgres-only: it scans ``note_links`` for unresolved
wikilinks and inserts ``Gap`` rows, with no vault filesystem and no stub
files. These tests use the same in-memory SQLite + schema-strip fixture as
``gap_lifecycle_test.py`` so table DDL works without a real Postgres.
"""

from __future__ import annotations

import inspect

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.gaps import GAPS_PIPELINE_VERSION, discover_gaps
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
    aliases: list[str] | None = None,
) -> Note:
    note = Note(
        note_id=note_id,
        path=f"_processed/{note_id}.md",
        title=title or note_id,
        content_hash=f"hash-{note_id}",
        type="atom",
        aliases=aliases or [],
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


def _live_gaps(session: Session) -> list[Gap]:
    return session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()


def test_signature_takes_only_session() -> None:
    """The fileless rewrite drops the ``vault_root`` param entirely."""
    params = list(inspect.signature(discover_gaps).parameters)
    assert params == ["session"]


def test_unresolved_wikilink_creates_one_discovered_gap(session) -> None:
    src = _make_note(session, "source-note", title="Source Note")
    _add_body_link(session, src_fk=src.id, target_id="Missing Concept")

    created = discover_gaps(session)

    assert created == 1
    gaps = _live_gaps(session)
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.term == "Missing Concept"
    assert gap.note_id == "missing-concept"
    assert gap.context == "Source Note"
    assert gap.state == "discovered"
    assert gap.gap_class is None
    assert gap.pipeline_version == GAPS_PIPELINE_VERSION


def test_link_resolved_by_note_id_creates_no_gap(session) -> None:
    src = _make_note(session, "source-note", title="Source")
    _make_note(session, "target-note", title="Target")
    _add_body_link(session, src_fk=src.id, target_id="target-note")

    created = discover_gaps(session)

    assert created == 0
    assert _live_gaps(session) == []


def test_link_resolved_by_alias_creates_no_gap(session) -> None:
    """A wikilink slugging to a canonical atom's alias is not a gap."""
    src = _make_note(session, "source-note", title="Source")
    _make_note(
        session,
        "probability-update-rule",
        title="Probability Update Rule",
        aliases=["Bayes' Theorem", "Bayes' Rule"],
    )
    # Body text ``[[Bayes' Theorem]]`` slugifies to ``bayes-theorem`` which
    # is covered by the canonical atom's alias; not a gap.
    _add_body_link(session, src_fk=src.id, target_id="Bayes' Theorem")

    created = discover_gaps(session)

    assert created == 0
    assert _live_gaps(session) == []


def test_existing_gap_for_term_is_not_duplicated(session) -> None:
    """UNIQUE(term): a term that already has a gap row is left as-is."""
    src = _make_note(session, "source-note", title="Source")
    session.add(
        Gap(
            term="Missing Concept",
            context="Source",
            note_id="missing-concept",
            pipeline_version=GAPS_PIPELINE_VERSION,
            state="discovered",
        )
    )
    session.commit()
    _add_body_link(session, src_fk=src.id, target_id="Missing Concept")

    created = discover_gaps(session)

    assert created == 0
    gaps = _live_gaps(session)
    assert len(gaps) == 1


def test_idempotent_second_run_inserts_nothing(session) -> None:
    src = _make_note(session, "src", title="Src")
    _add_body_link(session, src_fk=src.id, target_id="missing")

    assert discover_gaps(session) == 1
    assert discover_gaps(session) == 0
    assert len(_live_gaps(session)) == 1


def test_frontmatter_edges_are_not_gaps(session) -> None:
    """kind='edge' rows are typed assertions, not unresolved wikilinks."""
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

    assert discover_gaps(session) == 0
    assert _live_gaps(session) == []
