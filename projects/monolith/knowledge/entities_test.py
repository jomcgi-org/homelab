"""Tests for the repository-seeded knowledge entity spine."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge.entities import (
    Entity,
    NoteEntity,
    backfill_links,
    link_issue_entities,
    load_manifest,
    seed_entities,
)
from knowledge.models import Note


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'entities.db'}")
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


def _fact(
    note_id: str,
    title: str,
    *,
    content: str = "body",
    tags: list[str] | None = None,
    state: str = "unverified",
    deleted: bool = False,
) -> Note:
    return Note(
        note_id=note_id,
        path=f"{note_id}.md",
        title=title,
        content_hash=f"hash-{note_id}",
        content=content,
        type="fact",
        verification_state=state,
        tags=tags or [],
        deleted_at=datetime(2026, 9, 1, tzinfo=timezone.utc) if deleted else None,
    )


def test_manifest_loads_with_unique_kind_slug_keys():
    specs = load_manifest()

    keys = [(spec.kind, spec.slug) for spec in specs]
    assert len(keys) == len(set(keys))
    assert {spec.slug for spec in specs if spec.kind == "project"} >= {
        "embervm",
        "monolith",
        "tooling",
    }
    assert (
        next(spec for spec in specs if spec.slug == "tooling").title == "Build and CI"
    )


def test_seed_is_idempotent_and_merges_aliases(session):
    session.add(
        Entity(
            kind="project",
            slug="monolith",
            title="Old title",
            aliases=["local-alias"],
            source="manual",
        )
    )
    session.commit()

    first = seed_entities(session)
    second = seed_entities(session)

    monolith = session.exec(
        select(Entity).where(Entity.kind == "project", Entity.slug == "monolith")
    ).one()
    assert first.created == 22
    assert first.updated == 1
    assert second.created == 0
    assert second.updated == 0
    assert second.unchanged == 23
    assert "local-alias" in monolith.aliases
    assert monolith.aliases == sorted(monolith.aliases)
    assert monolith.source == "manifest"


def test_backfill_matches_tags_and_title_and_is_idempotent(session):
    seed_entities(session)
    session.add_all(
        [
            _fact("tag-match", "A generic fact", tags=["KG"]),
            _fact("title-match", "Qwen requests fail when capacity is full"),
            _fact("no-match", "A fact with no catalog vocabulary"),
            _fact("legacy", "Qwen legacy fact", state="legacy"),
            _fact("deleted", "Qwen deleted fact", deleted=True),
        ]
    )
    session.commit()

    dry_run = backfill_links(session, dry_run=True)
    first = backfill_links(session, dry_run=False)
    second = backfill_links(session, dry_run=False)

    assert dry_run.scanned == 3
    assert dry_run.linked == 2
    assert dry_run.unresolved == 1
    assert first.linked == 2
    assert first.unresolved == 1
    assert second.linked == 0
    links = session.exec(select(NoteEntity).order_by(NoteEntity.note_id)).all()
    assert [(link.note_id, link.role, link.source) for link in links] == [
        ("tag-match", "subject", "backfill"),
        ("title-match", "mentions", "backfill"),
    ]


def test_issue_linking_finds_title_and_content_references(session):
    session.add_all(
        [
            _fact("title-issue", "Fix #5899 before release"),
            _fact("body-issue", "Another fact", content="Tracked by #5527."),
            _fact("legacy-issue", "Old #5899", state="legacy"),
        ]
    )
    session.commit()

    assert link_issue_entities(session) == 2
    assert link_issue_entities(session) == 0

    issues = session.exec(
        select(Entity).where(Entity.kind == "issue").order_by(Entity.slug)
    ).all()
    assert [(entity.slug, entity.title, entity.source) for entity in issues] == [
        ("5527", "#5527", "regex"),
        ("5899", "#5899", "regex"),
    ]
    links = session.exec(select(NoteEntity).order_by(NoteEntity.note_id)).all()
    assert {(link.note_id, link.role) for link in links} == {
        ("body-issue", "mentions"),
        ("title-issue", "mentions"),
    }
