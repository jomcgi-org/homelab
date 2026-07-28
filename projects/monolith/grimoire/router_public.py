"""Public, read-only Grimoire HTTP API (Task 2).

Mounted ONLY on the public tier (see grimoire/__init__.py's register_public,
called from app/main_public.py). Never mounted on the private app, so there is
no collision with grimoire/router.py even though both use the same
``/api/grimoire`` prefix and several of the same path shapes: the private
router's paths all require a ``campaign``/``as`` query param this router never
accepts, and the two routers never share a FastAPI app instance.

No campaign, no viewer, no grants anywhere in this module: the whole corpus is
a single global read surface (Library, section tree, chunk reader, entity
index, entity detail, relationships, search). Books/sections/the image stream
reuse the corpus-global helpers already used by the private tier
(library.list_books/list_sections, the S3 streaming logic); everything else
is new in grimoire/public.py because the private equivalents are grant-shaped.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlmodel import Session

from core.db import get_session
from grimoire import explore, library, public
from grimoire.models import EntityType, KnowledgeChunk

router = APIRouter(prefix="/api/grimoire", tags=["grimoire-public"])


# --- Library / corpus (book + section reads, corpus-global) --------------


@router.get("/books")
def list_books(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Per-book coverage rows for the public Library. See library.list_books."""
    return library.list_books(session)


@router.get("/books/{book_id}/sections")
def list_book_sections(
    book_id: str, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    """Ordered section tree for one book (reading order, chunk counts)."""
    return library.list_sections(session, book_id)


@router.get("/books/{book_id}/read")
def read_book(
    book_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(
        default=library.DEFAULT_READ_PAGE, ge=1, le=library.MAX_READ_PAGE
    ),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Seq-ordered page of full chunks for the continuous public reader
    (corpus-global; same shape as the private endpoint).

    Copyrighted books are Reader-locked: their verbatim text is never served
    on the public tier (only the transformative Entities/Chat/Explore
    surfaces are corpus-wide). See library.is_book_copyrighted."""
    if library.is_book_copyrighted(session, book_id):
        raise HTTPException(status_code=403, detail="book text is not public")
    return library.read_page(session, book_id, cursor=cursor, limit=limit)


@router.get("/adventures")
def list_adventures_all(
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """All adventures across the corpus, for the EXPLORE gallery. See
    library.list_all_adventures."""
    return library.list_all_adventures(session)


@router.get("/books/{book_id}/adventures")
def list_adventures(
    book_id: str, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    """Adventures in a book, seq-ordered, each with entity_count. Empty list
    for the vast majority of books, which have no adventure rows. See
    library.list_adventures."""
    return library.list_adventures(session, book_id)


@router.get("/adventures/{adventure_id}")
def get_adventure(
    adventure_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """One adventure with its full entity roster, no grants. See
    library.adventure_entities."""
    adventure = library.adventure_entities(session, adventure_id)
    if adventure is None:
        raise HTTPException(status_code=404, detail="adventure not found")
    return adventure


# --- Chunk reader ----------------------------------------------------


@router.get("/chunks/{chunk_id}")
def get_chunk(chunk_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """One chunk with full content, image URL, seq neighbours, and every
    on-page entity mention, unfiltered (no campaign, no grants).

    Reader-locked for copyrighted books (see read_book): the book lookup here
    is an identity-map hit that get_chunk_public reuses, so it costs one PK
    fetch, not a second content query."""
    row = session.get(KnowledgeChunk, chunk_id)
    if row is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    if library.is_book_copyrighted(session, row.book_id):
        raise HTTPException(status_code=403, detail="book text is not public")
    chunk = public.get_chunk_public(session, chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    return chunk


def _parse_s3_uri(uri: str) -> tuple[str, str] | None:
    """Split ``s3://bucket/key`` into ``(bucket, key)``; None if malformed."""
    if not uri.startswith("s3://"):
        return None
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        return None
    return bucket, key


_IMAGE_CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


@router.get("/chunks/{chunk_id}/image")
def get_chunk_image(
    chunk_id: str, session: Session = Depends(get_session)
) -> StreamingResponse:
    """Stream the source illustration behind an image chunk's ``image_ref``.

    Same S3-stream implementation as the private router's endpoint of the
    same name (router.get_chunk_image): 404s on a missing chunk or a text
    chunk, reads the object from the shared SeaweedFS S3 endpoint. Kept as a
    literal copy rather than a shared import so the public tier never has to
    import grimoire.router (which pulls in the whole grant-filtered surface,
    campaign models, and the private embedding client dependency); the S3
    streaming logic itself has no grant concept to duplicate incorrectly.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    from grimoire.ingest import build_s3_client

    chunk = session.get(KnowledgeChunk, chunk_id)
    if chunk is None or not chunk.image_ref:
        raise HTTPException(status_code=404, detail="chunk image not found")
    # Reader-locked for copyrighted books: page scans are verbatim reproduction,
    # gated exactly like the text endpoints (read_book / get_chunk).
    if library.is_book_copyrighted(session, chunk.book_id):
        raise HTTPException(status_code=403, detail="book text is not public")
    parsed = _parse_s3_uri(chunk.image_ref)
    if parsed is None:
        raise HTTPException(status_code=404, detail="chunk image not found")
    bucket, key = parsed

    suffix = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    content_type = _IMAGE_CONTENT_TYPES.get(suffix, "application/octet-stream")

    client = build_s3_client()
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except (ClientError, BotoCoreError) as exc:
        # A missing object (ClientError) or any S3 transport error becomes a 404
        # so a broken image_ref never 500s the reader.
        raise HTTPException(status_code=404, detail="chunk image not found") from exc

    body = obj["Body"]

    def _stream():
        try:
            for part in body.iter_chunks(chunk_size=64 * 1024):
                yield part
        finally:
            body.close()

    return StreamingResponse(_stream(), media_type=content_type)


# --- Entities (no grants: the whole corpus is one global view) -----------


@router.get("/entities")
def list_entities(
    entity_type: EntityType | None = Query(default=None, alias="type"),
    q: str | None = Query(default=None),
    limit: int = Query(
        default=public.DEFAULT_ENTITY_PAGE, ge=1, le=public.MAX_ENTITY_PAGE
    ),
    cursor: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Paginated entity list, name-ordered, no grants. See
    public.list_entities_public for the pagination/secondary-field contract."""
    return public.list_entities_public(
        session, entity_type=entity_type, q=q, limit=limit, cursor=cursor
    )


@router.get("/entities/{entity_id}")
def get_entity(
    entity_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Full spine + typed detail for one entity, no grants."""
    entity = public.get_entity_public(session, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return entity


@router.get("/entities/{entity_id}/mentions")
def list_entity_mentions(
    entity_id: str, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    """Chunks that mention this entity (the "Sources" list on entity detail).

    404s if the entity itself does not exist, matching get_entity's
    not-found semantics; chunks are corpus-global so no further filtering
    applies once the entity is confirmed to exist.
    """
    if public.get_entity_public(session, entity_id) is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return library.list_entity_mentions(session, entity_id)


@router.get("/entities/{entity_id}/relationships")
def list_entity_relationships(
    entity_id: str, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    """1-hop relationship edges from/to entity_id, no recognition dimming."""
    if public.get_entity_public(session, entity_id) is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return public.list_relationships_public(session, entity_id)


# --- Search ---------------------------------------------------------


@router.get("/search")
def search(
    q: str = Query(min_length=1), session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Name + lore search over the whole corpus, no grants.

    Name-only in this PR (not the private tier's vector search): see
    public.search_public's docstring for why (the embedding inference
    service is not reachable from the public tier's network path today).
    """
    return public.search_public(session, q)


# --- EXPLORE (corpus-graph canvas: subgraph, ego expansion, pathfinding) --


@router.get("/explore/graph")
def explore_graph(
    scope: str = Query(default="everything"),
    lens: str = Query(default="world"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Induced subgraph ``{nodes, edges}`` for a scope + lens: the EXPLORE
    canvas's bulk load. ``scope`` is ``"everything"``, ``"adventure:{id}"``,
    or ``"book:{id}"``; ``lens`` is one of ``world``/``story``/``quests``/
    ``rules`` (anything else is unconstrained). A whole-corpus "everything"
    scope can be a large payload; the frontend should default to
    gallery/adventure scope. See explore.scope_subgraph."""
    return explore.scope_subgraph(session, scope, lens)


@router.get("/explore/ego")
def explore_ego(
    id: str = Query(...),
    scope: str = Query(default="everything"),
    lens: str = Query(default="world"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Focus entity + its 1-hop is_global neighbors, same ``{nodes, edges,
    lens_counts}`` shape as /explore/graph, for click-to-expand ("wander").
    ``scope``/``lens`` narrow the neighbor set the same way they narrow
    /explore/graph; the focus node is always kept. See
    explore.ego_subgraph."""
    return explore.ego_subgraph(session, id, scope, lens)


@router.get("/explore/path")
def explore_path(
    from_: str = Query(alias="from"),
    to: str = Query(...),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Six-degrees pathfinding between two entities: BFS over the
    relationship graph (both directions), is_global entities only, bounded
    to a fixed depth. See explore.shortest_path for the exact bound and the
    ordered-chain response shape."""
    return explore.shortest_path(session, from_, to)
