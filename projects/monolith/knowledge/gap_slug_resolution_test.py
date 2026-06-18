"""Regression tests for the wikilink-target slug resolution in
``knowledge.gaps.discover_gaps``.

``links.extract`` writes ``NoteLink.target_id`` as the raw wikilink text
(e.g. ``"Steve Krug"``), while ``existing_note_ids`` is built from
``Note.note_id`` slugs (``"steve-krug"``) plus slugified aliases. The
membership check in Phase 1 must slugify the target before comparing,
otherwise ``[[Title Case]]`` wikilinks to existing slug-named notes
create false-positive Gap rows -- the loop that previously sent
~10 already-resolved terms per tick to Sonnet for nothing.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.gaps import discover_gaps
from knowledge.models import Gap, Note, NoteLink


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


def _make_atom(
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


def test_raw_wikilink_resolves_to_existing_slug_note(session: Session) -> None:
    """A ``[[Title Case]]`` wikilink to a note with ``note_id="title-case"``
    must NOT produce a Gap row.

    Pre-fix: the lookup compared raw wikilink text to slugs and almost
    always missed, queueing the gap. Post-fix: ``_slugify(target_id)`` on
    the lookup side resolves the wikilink.
    """
    _make_atom(session, "steve-krug", title="Steve Krug")
    src = _make_atom(session, "src", title="Source")
    # Target stored in raw form, as ``links.extract`` emits it.
    _add_body_link(session, src_fk=src.id, target_id="Steve Krug")

    discover_gaps(session)

    rows = session.execute(select(Gap)).scalars().all()
    assert rows == [], (
        "Expected no Gap (resolves to existing 'steve-krug'), got: "
        f"{[(r.term, r.note_id) for r in rows]}"
    )


def test_raw_wikilink_resolves_via_existing_alias(session: Session) -> None:
    """``[[Bayes' Theorem]]`` should resolve to a canonical atom that
    carries ``Bayes' Theorem`` in its aliases, even though the wikilink
    text doesn't slug-match the canonical ``note_id``.
    """
    _make_atom(
        session,
        "thomas-bayes-rule",
        title="Thomas Bayes' Rule",
        aliases=["Bayes' Theorem"],
    )
    src = _make_atom(session, "src", title="Source")
    _add_body_link(session, src_fk=src.id, target_id="Bayes' Theorem")

    discover_gaps(session)

    rows = session.execute(select(Gap)).scalars().all()
    assert rows == [], (
        "Expected no Gap (resolves via alias), got: "
        f"{[(r.term, r.note_id) for r in rows]}"
    )


def test_raw_wikilink_with_no_match_still_creates_gap(session: Session) -> None:
    """Sanity check: when the slugified wikilink doesn't match any existing
    note or alias, the gap is correctly created."""
    src = _make_atom(session, "src", title="Source")
    _add_body_link(session, src_fk=src.id, target_id="Genuinely Novel Term")

    discover_gaps(session)

    rows = session.execute(select(Gap)).scalars().all()
    assert len(rows) == 1
    assert rows[0].term == "Genuinely Novel Term"
    assert rows[0].note_id == "genuinely-novel-term"
