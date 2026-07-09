"""Read-path aggregations for the Library and chunk reader (UI overhaul Phase 2).

The entity read paths (router.py) answer "what does this viewer know"; this
module answers the complementary "what is in the corpus and how far has
ingestion/extraction gotten" that the Library surface needs: per-book coverage
counts, the section tree, paged chunk lists, a single chunk with its neighbours
and on-page entities, and entity->chunk provenance.

Chunks are corpus-global in v1 (matching search._resolve_chunk_hit), so book,
section, and chunk reads are NOT campaign-scoped. Only the projections that
depend on a viewpoint (a chunk's on-page entity chips, an entity's mention list)
take a campaign + viewer, and those route every visibility decision through the
same visibility.visible_entities_query()/project_entity() helpers the entity
endpoints use, so the grant predicate stays in one place.

Every function here is a plain sync helper over a Session (no FastAPI, no
async): the GET endpoints that call them are sync ``def`` handlers, which
FastAPI runs in its threadpool, so blocking DB work never touches the event
loop (the same reason there is no ``asyncio.to_thread`` dance here that
ingest/extract need for their scheduler-loop handlers).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_
from sqlmodel import Session, select

from grimoire.extract import book_kind, current_extraction_key
from grimoire.models import (
    Adventure,
    Book,
    ChunkEntityMention,
    ChunkExtraction,
    Entity,
    KnowledgeChunk,
)
from grimoire.visibility import Viewer, project_entity, visible_entities_query

# Characters of chunk content shown in a list/preview row (matches
# search._CHUNK_PREVIEW_LEN so previews are consistent across surfaces).
_PREVIEW_LEN = 200

# Default and max page size for the paged chunk list.
DEFAULT_CHUNK_PAGE = 100
MAX_CHUNK_PAGE = 500

# Default and max page size for the continuous reader (full content per item,
# so pages are kept smaller than the preview list above).
DEFAULT_READ_PAGE = 40
MAX_READ_PAGE = 120


def _as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a possibly-naive datetime (SQLite tests round-trip TIMESTAMPTZ as
    naive) to tz-aware UTC, matching the ships/router.py serialization pattern.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    coerced = _as_utc(dt)
    return coerced.isoformat() if coerced else None


def _section_title(section_path: str | None) -> str:
    """A short human label for a section: the last path segment, or a stable
    placeholder for chunks with no section_path (front matter, stray blocks).
    """
    if not section_path:
        return "(no section)"
    return section_path.rsplit("/", 1)[-1].strip() or section_path


def _kind(chunk: KnowledgeChunk) -> str:
    return "image" if chunk.image_ref else "text"


def is_book_copyrighted(session: Session, book_id: str) -> bool:
    """Whether ``book_id``'s full text is copyrighted and so must never be
    served on the public Reader (/books/{id}/read, /chunks/{id}, .../image).

    The grimoire.book.copyrighted_content column is the authoritative gate.
    A missing book row returns True (fail closed: an unknown book is treated
    as copyrighted, so a not-yet-classified upload can never leak its text).
    """
    book = session.get(Book, book_id)
    return book is None or book.copyrighted_content


def list_books(session: Session) -> list[dict[str, Any]]:
    """Per-book coverage rows for the Library.

    Combines four cheap grouped scans (chunk counts, extraction coverage under
    the live model+prompt, distinct entity yield, book display names) into one
    row per book. ``extracted_count`` counts distinct chunks with a
    ``chunk_extraction`` marker for the current ``(model, prompt_version)`` key,
    so it ticks up as the extraction CronWorkflow runs and resets if the model or
    prompt version changes (mirroring extract._select_pending_chunks). Books appear if
    they have either a grimoire.book row or any chunk, so a freshly-registered
    empty book still shows.
    """
    chunk_rows = session.execute(
        select(
            KnowledgeChunk.book_id,
            func.count().label("chunk_count"),
            # count(col) counts non-NULL values, so this is the image-chunk count.
            func.count(KnowledgeChunk.image_ref).label("image_count"),
            func.max(KnowledgeChunk.created_at).label("latest_chunk_at"),
        ).group_by(KnowledgeChunk.book_id)
    ).all()
    chunk_by_book = {r.book_id: r for r in chunk_rows}

    model, prompt_version = current_extraction_key()
    extracted_rows = session.execute(
        select(
            KnowledgeChunk.book_id,
            func.count(func.distinct(ChunkExtraction.chunk_id)).label("n"),
        )
        .join(ChunkExtraction, ChunkExtraction.chunk_id == KnowledgeChunk.id)
        .where(
            ChunkExtraction.model == model,
            ChunkExtraction.prompt_version == prompt_version,
        )
        .group_by(KnowledgeChunk.book_id)
    ).all()
    extracted_by_book = {r.book_id: r.n for r in extracted_rows}

    entity_rows = session.execute(
        select(
            KnowledgeChunk.book_id,
            func.count(func.distinct(ChunkEntityMention.entity_id)).label("n"),
        )
        .join(ChunkEntityMention, ChunkEntityMention.chunk_id == KnowledgeChunk.id)
        .group_by(KnowledgeChunk.book_id)
    ).all()
    entity_by_book = {r.book_id: r.n for r in entity_rows}

    books = {b.id: b for b in session.exec(select(Book)).all()}

    book_ids = set(chunk_by_book) | set(books)
    result: list[dict[str, Any]] = []
    for book_id in book_ids:
        book = books.get(book_id)
        chunks = chunk_by_book.get(book_id)
        result.append(
            {
                "book_id": book_id,
                "display_name": book.display_name if book else book_id,
                # Fail closed: a chunk-only book with no metadata row (book is
                # None) is treated as copyrighted, matching is_book_copyrighted.
                "copyrighted_content": book.copyrighted_content if book else True,
                "book_kind": book_kind(book_id),
                "chunk_count": chunks.chunk_count if chunks else 0,
                "image_count": chunks.image_count if chunks else 0,
                "extracted_count": extracted_by_book.get(book_id, 0),
                "entity_count": entity_by_book.get(book_id, 0),
                "last_loaded_at": _iso(book.created_at) if book else None,
                "latest_chunk_at": _iso(chunks.latest_chunk_at) if chunks else None,
            }
        )
    result.sort(key=lambda item: (item["display_name"].lower(), item["book_id"]))
    return result


def list_sections(session: Session, book_id: str) -> list[dict[str, Any]]:
    """Ordered section tree for one book.

    Sections are grouped by ``section_path`` in reading order (first appearance
    by ``seq``), not alphabetically. ``first_chunk_id`` is the earliest chunk in
    the section, so the reader can jump straight to the section start.
    """
    rows = session.execute(
        select(
            KnowledgeChunk.id,
            KnowledgeChunk.seq,
            KnowledgeChunk.section_path,
            KnowledgeChunk.image_ref,
            KnowledgeChunk.created_at,
        )
        .where(KnowledgeChunk.book_id == book_id)
        .order_by(KnowledgeChunk.seq, KnowledgeChunk.chunk_ref)
    ).all()

    sections: dict[str | None, dict[str, Any]] = {}
    order: list[str | None] = []
    for chunk_id, _seq, section_path, image_ref, created_at in rows:
        entry = sections.get(section_path)
        if entry is None:
            entry = {
                "section_path": section_path,
                "title": _section_title(section_path),
                "chunk_count": 0,
                "image_count": 0,
                "first_chunk_id": chunk_id,
                "latest_chunk_at": created_at,
            }
            sections[section_path] = entry
            order.append(section_path)
        entry["chunk_count"] += 1
        if image_ref:
            entry["image_count"] += 1
        current = entry["latest_chunk_at"]
        if created_at is not None and (current is None or created_at > current):
            entry["latest_chunk_at"] = created_at

    return [
        {**sections[key], "latest_chunk_at": _iso(sections[key]["latest_chunk_at"])}
        for key in order
    ]


def list_chunks(
    session: Session,
    book_id: str,
    *,
    section: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_CHUNK_PAGE,
) -> dict[str, Any]:
    """Seq-ordered page of a book's chunks (optionally within one section).

    Keyset paginated on ``seq`` (unique within a book), so ``cursor`` is simply
    the last seq returned; the next page is ``seq > cursor``. Returns
    ``{items, next_cursor}`` where ``next_cursor`` is null on the last page.
    """
    limit = max(1, min(limit, MAX_CHUNK_PAGE))
    query = select(KnowledgeChunk).where(KnowledgeChunk.book_id == book_id)
    if section is not None:
        query = query.where(KnowledgeChunk.section_path == section)
    if cursor is not None:
        try:
            after = int(cursor)
        except (TypeError, ValueError):
            after = None
        if after is not None:
            query = query.where(KnowledgeChunk.seq > after)
    # limit + 1 to detect whether another page follows without a second query.
    query = query.order_by(KnowledgeChunk.seq).limit(limit + 1)
    rows = session.exec(query).all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "id": chunk.id,
            "seq": chunk.seq,
            "section_path": chunk.section_path,
            "kind": _kind(chunk),
            "preview": chunk.content[:_PREVIEW_LEN],
            "created_at": _iso(chunk.created_at),
        }
        for chunk in rows
    ]
    # Guard against a NULL seq on the boundary row: stringifying it would yield
    # "None", and the next request's int("None") would fail, drop the cursor, and
    # re-serve page one forever. seq is backfilled + loader-set so this is an
    # edge case, but the column is nullable, so fail safe by ending the page.
    last_seq = rows[-1].seq if rows else None
    next_cursor = str(last_seq) if has_more and last_seq is not None else None
    return {"items": items, "next_cursor": next_cursor}


def _image_object_key(image_ref: str | None) -> str | None:
    """Bucket-relative S3 object key from a chunk's ``s3://bucket/key`` ref.

    The reader response carries the object key (not a URL): the SvelteKit
    server layer HMAC-signs it into a same-origin ``/img/<sig>/<preset>/...``
    imgproxy URL, keeping the signing secret out of both this API and the
    browser. Returns None for a malformed ref so a bad row degrades to a
    caption-only figure instead of a broken image request.
    """
    if not image_ref or not image_ref.startswith("s3://"):
        return None
    _bucket, _, key = image_ref[len("s3://") :].partition("/")
    return key or None


def read_page(
    session: Session,
    book_id: str,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_READ_PAGE,
) -> dict[str, Any]:
    """Seq-ordered page of FULL chunks for the continuous book reader.

    The reader reconstructs the book: complete text (not the preview
    ``list_chunks`` serves), and for image chunks the bucket-relative object
    key plus the caption as ``content``. Keyset paginated on ``seq`` exactly
    like ``list_chunks`` (cursor = last seq returned; NULL-seq boundary rows
    end the page rather than emitting a "None" cursor).
    """
    limit = max(1, min(limit, MAX_READ_PAGE))
    query = select(KnowledgeChunk).where(KnowledgeChunk.book_id == book_id)
    if cursor is not None:
        try:
            after = int(cursor)
        except (TypeError, ValueError):
            after = None
        if after is not None:
            query = query.where(KnowledgeChunk.seq > after)
    query = query.order_by(KnowledgeChunk.seq).limit(limit + 1)
    rows = session.exec(query).all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "id": chunk.id,
            "seq": chunk.seq,
            "section_path": chunk.section_path,
            "kind": _kind(chunk),
            "content": chunk.content,
            "image_key": _image_object_key(chunk.image_ref),
        }
        for chunk in rows
    ]
    last_seq = rows[-1].seq if rows else None
    next_cursor = str(last_seq) if has_more and last_seq is not None else None
    return {"items": items, "next_cursor": next_cursor}


def _project_page_entity(
    session: Session, campaign_id: str, viewer: Viewer, entity_id: str
) -> dict[str, Any] | None:
    """Project one on-page entity for the reader's "entities here" chips.

    Uses "relationship" context so a name_only grant yields a recognition stub
    (the player still sees the name they've heard) while a wholly-invisible
    entity (non-global, ungranted) is dropped, exactly like router._project_
    neighbor. The DM's grant-join can return multiple rows per entity; only the
    identity is needed for a chip, so the first row is enough.
    """
    rows = session.exec(
        visible_entities_query(campaign_id, viewer).where(Entity.id == entity_id)
    ).all()
    if not rows:
        return None
    entity, grant = rows[0]
    return project_entity(entity, None, grant, viewer, context="relationship")


def get_chunk(
    session: Session, campaign_id: str, viewer: Viewer, chunk_id: str
) -> dict[str, Any] | None:
    """One chunk with full content, image URL, seq neighbours, and on-page
    entities projected for ``viewer``. Returns None if the chunk is missing.

    prev/next walk the book's ``seq`` order (the reading order the loader
    assigns), so the reader's back/next controls follow the page sequence, not
    insertion order. Entities come from chunk_entity_mention, viewpoint-filtered.
    """
    chunk = session.get(KnowledgeChunk, chunk_id)
    if chunk is None:
        return None

    prev_id: str | None = None
    next_id: str | None = None
    if chunk.seq is not None:
        prev_id = session.exec(
            select(KnowledgeChunk.id)
            .where(
                KnowledgeChunk.book_id == chunk.book_id,
                KnowledgeChunk.seq < chunk.seq,
            )
            .order_by(KnowledgeChunk.seq.desc())
            .limit(1)
        ).first()
        next_id = session.exec(
            select(KnowledgeChunk.id)
            .where(
                KnowledgeChunk.book_id == chunk.book_id,
                KnowledgeChunk.seq > chunk.seq,
            )
            .order_by(KnowledgeChunk.seq)
            .limit(1)
        ).first()

    mention_rows = session.exec(
        select(Entity.id, ChunkEntityMention.mention_text)
        .join(ChunkEntityMention, ChunkEntityMention.entity_id == Entity.id)
        .where(ChunkEntityMention.chunk_id == chunk_id)
        .order_by(Entity.name)
    ).all()
    entities: list[dict[str, Any]] = []
    for entity_id, mention_text in mention_rows:
        projected = _project_page_entity(session, campaign_id, viewer, entity_id)
        if projected is not None:
            projected["mention_text"] = mention_text
            entities.append(projected)

    chunk_count = session.exec(
        select(func.count())
        .select_from(KnowledgeChunk)
        .where(KnowledgeChunk.book_id == chunk.book_id)
    ).one()

    return {
        "id": chunk.id,
        "book_id": chunk.book_id,
        "content": chunk.content,
        "section_path": chunk.section_path,
        "seq": chunk.seq,
        "chunk_count": chunk_count,
        # A relative URL to the streaming endpoint; null for text chunks so the
        # reader knows there is no image to render.
        "image_url": (
            f"/api/grimoire/chunks/{chunk.id}/image" if chunk.image_ref else None
        ),
        "prev_id": prev_id,
        "next_id": next_id,
        "entities": entities,
        "created_at": _iso(chunk.created_at),
    }


def list_entity_mentions(session: Session, entity_id: str) -> list[dict[str, Any]]:
    """Chunks that mention ``entity_id`` (the "Sources" list on entity detail).

    The caller (router) is responsible for gating on the entity being visible to
    the viewer before calling this; chunks themselves are global in v1, so no
    per-chunk visibility filtering happens here. Ordered by book then reading
    order so sources read top-to-bottom within each book.
    """
    rows = session.exec(
        select(KnowledgeChunk, ChunkEntityMention.mention_text)
        .join(ChunkEntityMention, ChunkEntityMention.chunk_id == KnowledgeChunk.id)
        .where(ChunkEntityMention.entity_id == entity_id)
        .order_by(KnowledgeChunk.book_id, KnowledgeChunk.seq)
    ).all()
    return [
        {
            "chunk_id": chunk.id,
            "book_id": chunk.book_id,
            "section_path": chunk.section_path,
            "mention_text": mention_text,
            "preview": chunk.content[:_PREVIEW_LEN],
        }
        for chunk, mention_text in rows
    ]


def _adventure_chunk_join(query):
    """Join condition shared by list_adventures/adventure_entities: a
    knowledge_chunk belongs to an adventure when it is in the same book and
    its seq falls in [start_seq, end_seq] (end_seq NULL = to end of book).
    This is the app-side mirror of grimoire.adventure_entity, computed here
    (not against the Postgres view) so SQLite test fixtures see identical
    behaviour: a view is invisible to SQLite's create_all.
    """
    return query.join(
        KnowledgeChunk,
        and_(
            KnowledgeChunk.book_id == Adventure.book_id,
            KnowledgeChunk.seq >= Adventure.start_seq,
            or_(
                Adventure.end_seq.is_(None),
                KnowledgeChunk.seq <= Adventure.end_seq,
            ),
        ),
    )


def list_adventures(session: Session, book_id: str) -> list[dict[str, Any]]:
    """Adventures in one book, seq-ordered, each with entity_count.

    entity_count is COUNT(DISTINCT entity_id) over the chunks in the
    adventure's seq range (see _adventure_chunk_join). Returns an empty list
    for the vast majority of books, which have no adventure rows at all.
    """
    adventures = session.exec(
        select(Adventure).where(Adventure.book_id == book_id).order_by(Adventure.seq)
    ).all()
    if not adventures:
        return []

    count_rows = session.execute(
        _adventure_chunk_join(
            select(
                Adventure.id,
                func.count(func.distinct(ChunkEntityMention.entity_id)),
            ).where(Adventure.book_id == book_id)
        )
        .join(ChunkEntityMention, ChunkEntityMention.chunk_id == KnowledgeChunk.id)
        .group_by(Adventure.id)
    ).all()
    entity_counts = dict(count_rows)

    return [
        {
            "id": a.id,
            "name": a.name,
            "seq": a.seq,
            "summary": a.summary,
            "level_range": a.level_range,
            "start_seq": a.start_seq,
            "end_seq": a.end_seq,
            "entity_count": entity_counts.get(a.id, 0),
        }
        for a in adventures
    ]


def list_all_adventures(session: Session) -> list[dict[str, Any]]:
    """Every adventure across all books, book-then-seq ordered, each with
    entity_count and its book's display_name. Powers the EXPLORE gallery
    (the corpus-wide sibling of list_adventures, which is scoped to one book).
    """
    count_rows = session.execute(
        _adventure_chunk_join(
            select(
                Adventure.id,
                func.count(func.distinct(ChunkEntityMention.entity_id)),
            )
        )
        .join(ChunkEntityMention, ChunkEntityMention.chunk_id == KnowledgeChunk.id)
        .group_by(Adventure.id)
    ).all()
    entity_counts = dict(count_rows)

    rows = session.exec(
        select(Adventure, Book.display_name)
        .join(Book, Book.id == Adventure.book_id)
        .order_by(Book.display_name, Adventure.seq)
    ).all()
    return [
        {
            "id": a.id,
            "book_id": a.book_id,
            "book_display_name": display_name,
            "name": a.name,
            "seq": a.seq,
            "summary": a.summary,
            "level_range": a.level_range,
            "start_seq": a.start_seq,
            "end_seq": a.end_seq,
            "entity_count": entity_counts.get(a.id, 0),
        }
        for a, display_name in rows
    ]


def adventure_entities(session: Session, adventure_id: str) -> dict[str, Any] | None:
    """One adventure's fields plus its DISTINCT entity roster.

    None if adventure_id does not exist. Entities are ordered by entity_type
    then name so the frontend can render grouped-by-type without a client-side
    sort. See _adventure_chunk_join for the seq-range join this mirrors from
    grimoire.adventure_entity.
    """
    adventure = session.get(Adventure, adventure_id)
    if adventure is None:
        return None
    book = session.get(Book, adventure.book_id)

    entity_rows = session.execute(
        _adventure_chunk_join(
            select(Entity.id, Entity.name, Entity.entity_type, Entity.category)
            .select_from(Adventure)
            .where(Adventure.id == adventure_id)
        )
        .join(ChunkEntityMention, ChunkEntityMention.chunk_id == KnowledgeChunk.id)
        .join(Entity, Entity.id == ChunkEntityMention.entity_id)
        .distinct()
        .order_by(Entity.entity_type, Entity.name)
    ).all()

    return {
        "id": adventure.id,
        "book_id": adventure.book_id,
        "book_display_name": book.display_name if book else adventure.book_id,
        "name": adventure.name,
        "seq": adventure.seq,
        "summary": adventure.summary,
        "level_range": adventure.level_range,
        "start_seq": adventure.start_seq,
        "end_seq": adventure.end_seq,
        "entities": [
            {"id": eid, "name": name, "entity_type": entity_type, "category": category}
            for eid, name, entity_type, category in entity_rows
        ],
    }


def rename_book(session: Session, book_id: str, display_name: str) -> Book | None:
    """Set a book's display_name. Returns the updated row, or None if no such
    book exists (the loader upserts a row per book, so a missing book means the
    id was never loaded)."""
    book = session.get(Book, book_id)
    if book is None:
        return None
    book.display_name = display_name
    session.add(book)
    session.commit()
    session.refresh(book)
    return book
