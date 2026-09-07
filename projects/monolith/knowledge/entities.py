"""Repository-seeded entity spine and deterministic fact linking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, Session, SQLModel, select

from knowledge.models import Note, _STRING_ARRAY

EntityKind = Literal["project", "service", "environment", "issue"]
EntityRole = Literal["subject", "mentions"]

_ENTITY_KINDS = frozenset({"project", "service", "environment", "issue"})
_ISSUE_RE = re.compile(r"#(\d{3,5})(?!\d)")
_BIGINT = BigInteger().with_variant(Integer, "sqlite")
_MANIFEST_PATH = Path(__file__).with_name("entities.yaml")
_BACKFILL_CHUNK = 500


class Entity(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    """A closed-vocabulary subject that knowledge facts can reference."""

    __tablename__ = "entities"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('project', 'service', 'environment', 'issue')",
            name="entities_kind_chk",
        ),
        UniqueConstraint("kind", "slug", name="entities_kind_slug_key"),
        {"schema": "knowledge", "extend_existing": True},
    )

    id: int | None = Field(
        default=None,
        sa_column=Column(_BIGINT, primary_key=True, autoincrement=True),
    )
    kind: EntityKind = Field(sa_column=Column(String, nullable=False))
    slug: str = Field(sa_column=Column(String, nullable=False))
    title: str = Field(sa_column=Column(String, nullable=False))
    aliases: list[str] = Field(
        default_factory=list,
        sa_column=Column(_STRING_ARRAY, nullable=False),
    )
    scope: str | None = None
    source: str = Field(sa_column=Column(String, nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )


class NoteEntity(SQLModel, table=True):
    """A stable-note-id link to an entity subject or mention."""

    __tablename__ = "note_entities"
    __table_args__ = (
        CheckConstraint(
            "role IN ('subject', 'mentions')",
            name="note_entities_role_chk",
        ),
        UniqueConstraint(
            "note_id",
            "entity_id",
            "role",
            name="note_entities_note_id_entity_id_role_key",
        ),
        {"schema": "knowledge", "extend_existing": True},
    )

    id: int | None = Field(
        default=None,
        sa_column=Column(_BIGINT, primary_key=True, autoincrement=True),
    )
    note_id: str = Field(sa_column=Column(String, nullable=False))
    entity_id: int = Field(
        sa_column=Column(
            _BIGINT,
            ForeignKey("knowledge.entities.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    role: EntityRole = Field(sa_column=Column(String, nullable=False))
    source: str = Field(sa_column=Column(String, nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )


@dataclass(frozen=True)
class EntitySpec:
    kind: EntityKind
    slug: str
    title: str
    aliases: tuple[str, ...]
    scope: str | None = None


@dataclass(frozen=True)
class SeedReport:
    created: int
    updated: int
    unchanged: int


@dataclass(frozen=True)
class BackfillReport:
    scanned: int
    linked: int
    unresolved: int
    dry_run: bool


def load_manifest() -> list[EntitySpec]:
    """Load and validate the committed entity manifest."""
    payload = yaml.safe_load(_MANIFEST_PATH.read_text())
    rows = payload.get("entities") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("entities.yaml must contain an entities list")

    specs: list[EntitySpec] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("every entity manifest entry must be a mapping")
        kind = row.get("kind")
        slug = row.get("slug")
        title = row.get("title")
        aliases = row.get("aliases")
        scope = row.get("scope")
        if kind not in _ENTITY_KINDS:
            raise ValueError(f"invalid entity kind: {kind!r}")
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("entity slug must be a non-empty string")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"entity {slug!r} must have a non-empty title")
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias.strip() for alias in aliases
        ):
            raise ValueError(f"entity {slug!r} aliases must be non-empty strings")
        if scope is not None and not isinstance(scope, str):
            raise ValueError(f"entity {slug!r} scope must be a string or null")
        key = (kind, slug)
        if key in seen:
            raise ValueError(f"duplicate entity key: {kind}/{slug}")
        seen.add(key)
        specs.append(
            EntitySpec(
                kind=kind,
                slug=slug,
                title=title,
                aliases=tuple(aliases),
                scope=scope,
            )
        )
    return specs


def seed_entities(session: Session) -> SeedReport:
    """Upsert manifest entities, preserving aliases learned outside the manifest."""
    specs = load_manifest()
    existing = {
        (entity.kind, entity.slug): entity
        for entity in session.exec(select(Entity)).all()
    }
    additions: list[Entity] = []
    created = 0
    updated = 0
    unchanged = 0
    now = datetime.now(timezone.utc)
    for spec in specs:
        entity = existing.get((spec.kind, spec.slug))
        if entity is None:
            additions.append(
                Entity(
                    kind=spec.kind,
                    slug=spec.slug,
                    title=spec.title,
                    aliases=sorted(set(spec.aliases)),
                    scope=spec.scope,
                    source="manifest",
                )
            )
            created += 1
            continue

        merged_aliases = sorted(set(entity.aliases or []) | set(spec.aliases))
        changed = (
            entity.title != spec.title
            or entity.aliases != merged_aliases
            or entity.scope != spec.scope
            or entity.source != "manifest"
        )
        if not changed:
            unchanged += 1
            continue
        entity.title = spec.title
        entity.aliases = merged_aliases
        entity.scope = spec.scope
        entity.source = "manifest"
        entity.updated_at = now
        updated += 1

    session.add_all(additions)
    session.commit()
    return SeedReport(created=created, updated=updated, unchanged=unchanged)


def _note_entity_insert(session: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    dialect = session.get_bind().dialect.name
    insert = sqlite_insert if dialect == "sqlite" else pg_insert
    statement = (
        insert(NoteEntity.__table__)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["note_id", "entity_id", "role"])
    )
    result = session.execute(statement)
    return (
        result.rowcount if result.rowcount is not None and result.rowcount >= 0 else 0
    )


def link_subjects(
    session: Session,
    note_id: str,
    slugs: list[str],
    *,
    source: str,
) -> tuple[int, list[str]]:
    """Link resolvable project slugs and return unknown slugs."""
    requested = list(
        dict.fromkeys(slug.strip().casefold() for slug in slugs if slug.strip())
    )
    if not requested:
        return 0, []
    entities = session.exec(
        select(Entity).where(Entity.kind == "project", Entity.slug.in_(requested))
    ).all()
    by_slug = {entity.slug.casefold(): entity for entity in entities}
    unresolved = [slug for slug in requested if slug not in by_slug]
    rows = [
        {
            "note_id": note_id,
            "entity_id": by_slug[slug].id,
            "role": "subject",
            "source": source,
        }
        for slug in requested
        if slug in by_slug
    ]
    return _note_entity_insert(session, rows), unresolved


def link_issue_entities(session: Session) -> int:
    """Create regex-derived issue entities and mention links for live facts."""
    notes = session.exec(
        select(Note).where(
            Note.type == "fact",
            Note.verification_state != "legacy",
            Note.deleted_at.is_(None),
        )
    ).all()
    issues_by_note = {
        str(note.note_id): set(_ISSUE_RE.findall(f"{note.title}\n{note.content or ''}"))
        for note in notes
    }
    issue_slugs = sorted({slug for slugs in issues_by_note.values() for slug in slugs})
    if not issue_slugs:
        return 0

    existing = {
        entity.slug: entity
        for entity in session.exec(
            select(Entity).where(Entity.kind == "issue", Entity.slug.in_(issue_slugs))
        ).all()
    }
    now = datetime.now(timezone.utc)
    for slug, entity in existing.items():
        expected_title = f"#{slug}"
        if entity.title != expected_title or entity.source != "regex":
            entity.title = expected_title
            entity.source = "regex"
            entity.updated_at = now
    additions = [
        Entity(
            kind="issue",
            slug=slug,
            title=f"#{slug}",
            aliases=[],
            source="regex",
        )
        for slug in issue_slugs
        if slug not in existing
    ]
    session.add_all(additions)
    session.flush()
    entities = {
        entity.slug: entity
        for entity in session.exec(
            select(Entity).where(Entity.kind == "issue", Entity.slug.in_(issue_slugs))
        ).all()
    }
    rows = [
        {
            "note_id": note_id,
            "entity_id": entities[slug].id,
            "role": "mentions",
            "source": "regex",
        }
        for note_id, slugs in issues_by_note.items()
        for slug in sorted(slugs)
    ]
    linked = _note_entity_insert(session, rows)
    session.commit()
    return linked


def _title_mentions(title: str, alias: str) -> bool:
    pattern = rf"(?<!\w){re.escape(alias)}(?!\w)"
    return re.search(pattern, title, flags=re.IGNORECASE) is not None


def backfill_links(session: Session, dry_run: bool) -> BackfillReport:
    """Deterministically link live, non-legacy facts in 500-note chunks."""
    entities = session.exec(select(Entity).order_by(Entity.kind, Entity.slug)).all()
    tag_lookup: dict[str, set[int]] = {}
    title_aliases: list[tuple[str, int]] = []
    for entity in entities:
        if entity.id is None:
            continue
        tag_terms = {entity.slug, *(entity.aliases or [])}
        for term in tag_terms:
            tag_lookup.setdefault(term.casefold(), set()).add(entity.id)
        title_aliases.extend((alias, entity.id) for alias in entity.aliases or [])

    scanned = 0
    linked = 0
    unresolved = 0
    last_id = 0
    while True:
        notes = session.exec(
            select(Note)
            .where(
                Note.id > last_id,
                Note.type == "fact",
                Note.verification_state != "legacy",
                Note.deleted_at.is_(None),
            )
            .order_by(Note.id)
            .limit(_BACKFILL_CHUNK)
        ).all()
        if not notes:
            break
        last_id = notes[-1].id or last_id
        scanned += len(notes)

        candidate_rows: list[dict] = []
        for note in notes:
            subject_ids: set[int] = set()
            for tag in note.tags or []:
                subject_ids.update(tag_lookup.get(tag.strip().casefold(), set()))
            mention_ids = {
                entity_id
                for alias, entity_id in title_aliases
                if _title_mentions(note.title, alias)
            } - subject_ids
            if not subject_ids and not mention_ids:
                unresolved += 1
            candidate_rows.extend(
                {
                    "note_id": str(note.note_id),
                    "entity_id": entity_id,
                    "role": "subject",
                    "source": "backfill",
                }
                for entity_id in sorted(subject_ids)
            )
            candidate_rows.extend(
                {
                    "note_id": str(note.note_id),
                    "entity_id": entity_id,
                    "role": "mentions",
                    "source": "backfill",
                }
                for entity_id in sorted(mention_ids)
            )

        if dry_run:
            note_ids = [str(note.note_id) for note in notes]
            existing = {
                (row.note_id, row.entity_id, row.role)
                for row in session.exec(
                    select(NoteEntity).where(NoteEntity.note_id.in_(note_ids))
                ).all()
            }
            linked += sum(
                (row["note_id"], row["entity_id"], row["role"]) not in existing
                for row in candidate_rows
            )
            continue

        linked += _note_entity_insert(session, candidate_rows)
        session.commit()

    return BackfillReport(
        scanned=scanned,
        linked=linked,
        unresolved=unresolved,
        dry_run=dry_run,
    )
