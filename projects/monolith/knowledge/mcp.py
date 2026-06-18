"""MCP tools for knowledge graph search, note management, and task tracking.

Registers note tools (``search_knowledge``, ``get_note``, ``create_note``,
``edit_note``, ``delete_note``) and task tools (``list_tasks``,
``search_tasks``, ``update_task``, ``get_daily_tasks``, ``get_weekly_tasks``)
on the shared monolith MCP instance.
Tools call KnowledgeStore directly (no HTTP round-trip).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from sqlmodel import Session, select

from app.db import get_engine
from app.mcp_app import mcp
from knowledge import frontmatter
from knowledge import notes as notes_module
from knowledge.gaps import answer_gap as _answer_gap
from knowledge.gaps import list_review_queue, resolve_gaps_for_note, split_csv
from knowledge.gaps import set_gap_class as _set_gap_class
from knowledge.gardener import GARDENER_VERSION, _slugify
from knowledge.indexing import index_note_best_effort, index_note_from_raw
from knowledge.models import AtomRawProvenance, RawInput
from knowledge.notes import DEFAULT_VAULT_ROOT, VAULT_ROOT_ENV, resolve_note_body
from knowledge.raw_store import fetch_raw
from knowledge.store import KnowledgeStore
from shared.embedding import EmbeddingClient

logger = logging.getLogger(__name__)


@mcp.tool
async def search_knowledge(
    query: str,
    limit: int = 20,
    type: str | None = None,
) -> dict:
    """Semantic search over the knowledge graph.

    Embeds the query and searches notes by cosine similarity.
    Returns ranked results with title, type, tags, best-matching
    section, a 240-char snippet, and graph edges.

    Args:
        query: Natural language search query (minimum 2 characters).
        limit: Maximum results to return (default 20, max 100).
        type: Optional note type filter (e.g. "concept", "paper").
    """
    if len(query) < 2:
        return {"results": []}

    embed_client = EmbeddingClient()
    try:
        vector = await embed_client.embed(query)
    except Exception:
        logger.exception("knowledge mcp: embedding call failed")
        return {"error": "embedding unavailable"}

    with Session(get_engine()) as session:
        results = KnowledgeStore(session).search_notes_with_context(
            query_embedding=vector,
            limit=min(limit, 100),
            type_filter=type,
        )
    return {"results": results}


@mcp.tool
async def get_note(note_id: str) -> dict:
    """Retrieve a knowledge note by its stable ID.

    Returns note metadata (title, type, tags), the full markdown
    body (authoritative ``knowledge.notes.content`` in Postgres, with a
    vault fallback during the ADR 006 Phase 1 backfill), and all
    outgoing graph edges.

    Args:
        note_id: The stable note identifier (e.g. "attention-is-all-you-need").
    """
    with Session(get_engine()) as session:
        store = KnowledgeStore(session)
        note = store.get_note_by_id(note_id)
        if note is None:
            return {"error": f"note not found: {note_id}"}

        # ADR 006 Phase 2: body of record is Postgres ``content``; the vault
        # read is a fallback only while the Phase 1 backfill is in flight.
        vault_root = Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT)).resolve()
        body = resolve_note_body(note.get("content"), note["path"], vault_root)
        if body is None:
            return {"error": f"vault file missing for {note_id}"}

        edges = store.get_note_links(note_id)
        return {**note, "content": body, "edges": edges}


@mcp.tool
async def create_note(
    content: str,
    title: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    type: str | None = None,
    visibility: str | None = None,
) -> dict:
    """Create a new knowledge note in the vault.

    Writes a markdown file with YAML frontmatter to the vault root.
    The file is named from a slugified title with collision handling.

    Args:
        content: The markdown body of the note (required, must not be empty).
        title: Note title (defaults to first 60 characters of content).
        source: Optional source URL or reference.
        tags: Optional list of tags.
        type: Optional note type (e.g. "concept", "paper").
        visibility: Optional public or private. Atoms without visibility
            land in the review queue. Set explicitly to avoid that.
    """
    if not content or not content.strip():
        return {"error": "content must not be empty"}

    if visibility is not None and visibility not in ("public", "private"):
        return {"error": (f"visibility must be public or private, got {visibility!r}")}

    if title is None:
        title = content.strip()[:60]

    fm: dict[str, object] = {"title": title}
    if source:
        fm["source"] = source
    if tags:
        fm["tags"] = tags
    if type:
        fm["type"] = type
    if visibility:
        fm["visibility"] = visibility

    file_content = "---\n" + yaml.dump(fm, default_flow_style=False) + "---\n" + content

    vault_root = Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT)).resolve()
    slug = _slugify(title)
    candidate = vault_root / f"{slug}.md"
    counter = 1
    while candidate.exists():
        candidate = vault_root / f"{slug}-{counter}.md"
        counter += 1

    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(file_content)
    return {"path": candidate.name}


@mcp.tool
async def edit_note(
    note_id: str,
    content: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    visibility: str | None = None,
) -> dict:
    """Edit an existing knowledge note.

    Looks up the note by ID, merges the provided fields into the
    existing frontmatter, and writes the updated file back. All
    pre-existing frontmatter fields (visibility, source_tier, aliases,
    edges, created/updated timestamps, custom extras) are preserved
    on rewrite -- only the fields passed explicitly are modified.

    Args:
        note_id: The stable note identifier.
        content: New markdown body (replaces existing body if provided).
        title: New title (updates frontmatter if provided).
        tags: New tags list (updates frontmatter if provided).
        visibility: Optional public or private. When None, the
            existing visibility (if any) is preserved on rewrite.
    """
    if visibility is not None and visibility not in ("public", "private"):
        return {"error": (f"visibility must be public or private, got {visibility!r}")}

    with Session(get_engine()) as session:
        store = KnowledgeStore(session)
        note = store.get_note_by_id(note_id)
        if note is None:
            return {"error": f"note not found: {note_id}"}

        vault_root = Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT)).resolve()
        resolved = (vault_root / note["path"]).resolve()
        if not resolved.is_relative_to(vault_root) or not resolved.is_file():
            return {"error": f"vault file missing for {note_id}"}

        raw = resolved.read_text()
        parsed, body = frontmatter.parse(raw)

        if title is not None:
            parsed.title = title
        if tags is not None:
            parsed.tags = tags
        if content is not None:
            body = content
        if visibility is not None:
            parsed.visibility = visibility

        # Mirrors router.edit_note's field list so every frontmatter field the
        # parser knows about gets re-emitted. Drift between this list and the
        # HTTP edit_note is the bug that silently nulled visibility on
        # thousands of notes before this fix landed.
        fm_dict: dict[str, object] = {}
        if parsed.note_id:
            fm_dict["id"] = parsed.note_id
        if parsed.title:
            fm_dict["title"] = parsed.title
        if parsed.type:
            fm_dict["type"] = parsed.type
        if parsed.status:
            fm_dict["status"] = parsed.status
        if parsed.visibility is not None:
            fm_dict["visibility"] = parsed.visibility
        if parsed.source:
            fm_dict["source"] = parsed.source
        if parsed.tags:
            fm_dict["tags"] = parsed.tags
        if parsed.aliases:
            fm_dict["aliases"] = parsed.aliases
        if parsed.edges:
            fm_dict["edges"] = parsed.edges
        if parsed.created is not None:
            fm_dict["created"] = parsed.created.isoformat()
        if parsed.updated is not None:
            fm_dict["updated"] = parsed.updated.isoformat()
        if parsed.extra:
            fm_dict.update(parsed.extra)

        file_content = (
            "---\n" + yaml.dump(fm_dict, default_flow_style=False) + "---\n" + body
        )
        resolved.write_text(file_content)
        # ADR 006 Phase 3: re-index synchronously into Postgres; the disk
        # write above is the safety net.
        await index_note_best_effort(
            store,
            EmbeddingClient(),
            note_id=note_id,
            rel_path=note["path"],
            raw=file_content,
        )
        return {"path": note["path"], "note_id": note_id}


@mcp.tool
async def delete_note(note_id: str) -> dict:
    """Soft-delete a knowledge note.

    Moves the markdown file to the vault _trash directory and sets
    deleted_at on the row. The note disappears from all user-facing
    read paths (review queue, graph, search) but the row survives so
    undelete_note can restore the file to its original location.

    Args:
        note_id: The stable note identifier.
    """
    with Session(get_engine()) as session:
        vault_root = Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT)).resolve()
        try:
            note = notes_module.delete_note(session, note_id, vault_root)
        except ValueError as exc:
            return {"error": str(exc)}
        return {"deleted": True, "note_id": note.note_id}


# ---------------------------------------------------------------------------
# Task tools
# ---------------------------------------------------------------------------


@mcp.tool
async def list_tasks(
    status: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    size: str | None = None,
    include_someday: bool = False,
) -> dict:
    """List tasks with optional filters.

    Returns tasks sorted by most recently indexed. Someday tasks are
    excluded by default.

    Args:
        status: Comma-separated status filter (e.g. "todo,in-progress").
        due_before: ISO date — only tasks due on or before this date.
        due_after: ISO date — only tasks due on or after this date.
        size: Comma-separated size filter (e.g. "small,medium").
        include_someday: Include tasks with status "someday" (default false).
    """
    with Session(get_engine()) as session:
        tasks = KnowledgeStore(session).list_tasks(
            statuses=status.split(",") if status else None,
            due_before=due_before,
            due_after=due_after,
            sizes=size.split(",") if size else None,
            include_someday=include_someday,
        )
    return {"tasks": tasks}


@mcp.tool
async def search_tasks(
    query: str,
    status: str | None = None,
    include_someday: bool = False,
    limit: int = 20,
) -> dict:
    """Semantic search over tasks.

    Embeds the query and searches task notes by cosine similarity.

    Args:
        query: Natural language search query (minimum 2 characters).
        status: Comma-separated status filter (e.g. "todo,in-progress").
        include_someday: Include tasks with status "someday" (default false).
        limit: Maximum results to return (default 20).
    """
    if len(query) < 2:
        return {"tasks": []}

    embed_client = EmbeddingClient()
    try:
        vector = await embed_client.embed(query)
    except Exception:
        logger.exception("tasks mcp: embedding call failed")
        return {"error": "embedding unavailable"}

    with Session(get_engine()) as session:
        tasks = KnowledgeStore(session).search_tasks(
            query_embedding=vector,
            statuses=status.split(",") if status else None,
            include_someday=include_someday,
            limit=limit,
        )
    return {"tasks": tasks}


@mcp.tool
async def update_task(
    note_id: str,
    fields: dict,
) -> dict:
    """Update fields on a task.

    Merges the provided fields into the task's metadata. Automatically
    sets ``task-completed`` date when status transitions to done/cancelled,
    and clears it when moving away from those statuses.

    Args:
        note_id: The stable task identifier.
        fields: Dictionary of fields to update (e.g. {"status": "done"}).
    """
    with Session(get_engine()) as session:
        store = KnowledgeStore(session)
        try:
            store.patch_task(note_id, fields)
        except ValueError as exc:
            return {"error": str(exc)}
    return {"updated": True, "note_id": note_id}


@mcp.tool
async def get_daily_tasks() -> dict:
    """Get tasks due today or overdue.

    Returns tasks with a due date on or before today, excluding
    someday tasks.
    """
    with Session(get_engine()) as session:
        tasks = KnowledgeStore(session).list_tasks_daily()
    return {"tasks": tasks}


@mcp.tool
async def get_weekly_tasks() -> dict:
    """Get tasks due this week.

    Returns tasks with a due date between now and the end of the
    current week (Sunday), excluding someday tasks.
    """
    with Session(get_engine()) as session:
        tasks = KnowledgeStore(session).list_tasks_weekly()
    return {"tasks": tasks}


# ---------------------------------------------------------------------------
# Gap lifecycle tools
# ---------------------------------------------------------------------------


@mcp.tool
async def list_gaps(
    state: str | None = None,
    gap_class: str | None = None,
    limit: int = 100,
) -> dict:
    """List gaps in the knowledge graph with optional filters.

    Returns gaps sorted by most recently created.

    Args:
        state: Comma-separated state filter (e.g. "in_review,classified").
        gap_class: Comma-separated class filter (e.g. "internal,hybrid").
        limit: Maximum results to return (default 100, clamped to [1, 500]).
    """
    limit = max(1, min(500, limit))
    with Session(get_engine()) as session:
        gaps = KnowledgeStore(session).list_gaps(
            states=split_csv(state),
            classes=split_csv(gap_class),
            limit=limit,
        )
    return {"gaps": gaps}


@mcp.tool
async def get_review_queue() -> dict:
    """Return internal/hybrid gaps awaiting a user answer, oldest first.

    Use this to see which gaps need your attention. Use ``list_gaps``
    with explicit filters for anything else.
    """
    with Session(get_engine()) as session:
        return {"gaps": list_review_queue(session)}


@mcp.tool
async def answer_gap(gap_id: int, answer: str) -> dict:
    """Answer an in-review gap, emitting a personal-tier atom fileless.

    Indexes a new atom straight into Postgres with source_tier personal and
    visibility private, then marks the gap as committed. No filesystem.

    Args:
        gap_id: The id of a gap currently in state in_review.
        answer: The user's answer text. May not contain a frontmatter
            terminator (a line containing only three dashes).
    """
    with Session(get_engine()) as session:
        try:
            return await _answer_gap(session, gap_id, answer)
        except ValueError as exc:
            return {"error": str(exc)}


@mcp.tool
async def set_gap_class(gap_id: int, gap_class: str) -> dict:
    """Classify a discovered gap and transition its state fileless.

    Use this from the classification routine to route a gap to one of
    external, internal, hybrid, or parked. The gap must currently be in
    state discovered. External gaps stay discovered so the research routine
    can pull them, internal and hybrid gaps move to in_review for a user
    answer, and parked gaps become terminal.

    Args:
        gap_id: The id of a gap currently in state discovered.
        gap_class: One of external, internal, hybrid, or parked.
    """
    with Session(get_engine()) as session:
        try:
            return _set_gap_class(session, gap_id, gap_class)
        except ValueError as exc:
            return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Gardener decomposition tools (ADR 006 Phase 4c)
#
# The gardener is moving from an in-pod subprocess to a remote claude.ai
# routine that drives decomposition over these MCP tools. New atoms are
# FILELESS: written to Postgres only (no _processed file), because the
# reconciler permanently defers without an Obsidian sidecar.
# ---------------------------------------------------------------------------

_ATOM_TYPES = frozenset({"atom", "fact", "active"})
_VISIBILITIES = frozenset({"public", "private"})
_ACTIVE_STATUSES = frozenset({"active", "someday", "blocked"})
_ACTIVE_SIZES = frozenset({"small", "medium", "large", "unknown"})


@mcp.tool
async def list_raws_needing_decomposition(limit: int = 10) -> dict:
    """List raw inputs the gardener still needs to decompose, fresh first.

    Mirrors the in-pod gardener work queue: tier 1 is fresh raws with no
    current-version provenance, tier 2 is retriable failed raws under the
    retry ceiling. Use ``get_raw`` to read a raw body, then ``create_atom``
    to emit atoms and ``record_provenance`` to close out raws that yield no
    new notes or that fail.

    Args:
        limit: Maximum raws to return (default 10, clamped to 1 through 50).
    """
    limit = max(1, min(limit, 50))
    with Session(get_engine()) as session:
        raws = KnowledgeStore(session).raws_needing_decomposition(limit)
        out = []
        for raw in raws:
            # The body lives in S3 now (ADR 006 Phase 4d); this is a work-queue
            # listing, so skip the per-row S3 fetch and use the stable raw_id as
            # the display label. The gardener calls get_raw next to read the body.
            out.append(
                {
                    "raw_id": raw.raw_id,
                    "title": raw.raw_id,
                    "source": raw.source,
                    "created_at": raw.created_at,
                }
            )
    return {"raws": out}


@mcp.tool
async def get_raw(raw_id: str) -> dict:
    """Read a raw input markdown content by its ``raw_id``.

    Returns the body the gardener should decompose into atoms, fetched from
    object storage (``s3://knowledge/raws/<content_hash>.md``). The
    ``raw_inputs`` row holds only metadata plus the content hash (ADR 006 Phase 4d).

    Args:
        raw_id: The stable raw input identifier.
    """
    with Session(get_engine()) as session:
        row = session.exec(select(RawInput).where(RawInput.raw_id == raw_id)).first()
        if row is None:
            return {"error": f"raw not found: {raw_id}"}
        content_hash = row.content_hash
        source = row.source
    content = fetch_raw(content_hash)
    if content is None:
        return {"error": f"raw content not in object storage: {raw_id}"}
    return {"raw_id": raw_id, "content": content, "source": source}


async def _index_atom(
    session: Session,
    *,
    title: str,
    body: str,
    type: str,
    visibility: str,
    source_tier: str | None = None,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    edges: dict[str, list[str]] | None = None,
    derived_from_raw: str | None = None,
    status: str | None = None,
    size: str | None = None,
    due: str | None = None,
    blocked_by: list[str] | None = None,
) -> str:
    """Build and index a fileless atom into Postgres, returning its note_id.

    The shared core behind both create_atom (gardener and research routine)
    and answer_gap (user-answered gaps). Resolves a DB-unique note_id from the
    slugified title, serializes frontmatter, and indexes the note under the
    open session. source_tier is emitted only when provided (create_atom never
    sets it, answer_gap sets personal).

    The caller owns the gap-resolution step (resolve_gaps_for_note): it lives
    in create_atom only, so callers that manage their own gap state
    (answer_gap) do not double-resolve.
    """
    store = KnowledgeStore(session)

    # Resolve a unique note_id against the DB (fileless: no filesystem
    # collision check). The slug stem becomes the stable id.
    base = _slugify(title)
    note_id = base
    counter = 1
    while store.get_note_by_id(note_id) is not None:
        note_id = f"{base}-{counter}"
        counter += 1

    fm_dict: dict[str, object] = {
        "id": note_id,
        "title": title,
        "type": type,
        "visibility": visibility,
    }
    if source_tier is not None:
        fm_dict["source_tier"] = source_tier
    if derived_from_raw is not None:
        fm_dict["derived_from_raw"] = derived_from_raw
    if tags:
        fm_dict["tags"] = list(tags)
    if aliases:
        fm_dict["aliases"] = list(aliases)
    if edges:
        fm_dict["edges"] = {k: list(v) for k, v in edges.items()}
    if type == "active":
        fm_dict["status"] = status
        fm_dict["size"] = size
        if due is not None:
            fm_dict["due"] = due
        if blocked_by:
            fm_dict["blocked_by"] = list(blocked_by)

    fm_str = yaml.dump(fm_dict, default_flow_style=False, sort_keys=False)
    raw = f"---\n{fm_str}---\n\n{body.strip()}\n"

    await index_note_from_raw(
        store,
        EmbeddingClient(),
        note_id=note_id,
        rel_path=f"_processed/{note_id}.md",
        raw=raw,
    )
    return note_id


@mcp.tool
async def create_atom(
    title: str,
    body: str,
    type: str,
    visibility: str,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    edges: dict[str, list[str]] | None = None,
    derived_from_raw: str | None = None,
    status: str | None = None,
    size: str | None = None,
    due: str | None = None,
    blocked_by: list[str] | None = None,
) -> dict:
    """Create a new knowledge atom, fileless, indexed straight into Postgres.

    Validates the atom schema, slugifies the title into a stable ``note_id``
    (resolving DB collisions with a numeric suffix), serializes frontmatter,
    and indexes the note synchronously. When ``derived_from_raw`` resolves
    to a raw input, an ``AtomRawProvenance`` row links the atom to its raw.

    Validation failures return an error message and write nothing.

    Args:
        title: Human-readable note title (required).
        body: Markdown body of the atom (required).
        type: One of atom, fact, or active.
        visibility: One of public or private.
        tags: Optional list of tags.
        aliases: Optional list of aliases.
        edges: Optional typed edges, e.g. a derives_from list of note ids.
        derived_from_raw: Optional raw_id to record provenance against.
        status: Required for type active. One of active, someday, blocked.
        size: Required for type active. One of small, medium, large, unknown.
        due: Optional ISO due date (active notes only).
        blocked_by: Optional list of blocking note_ids (active notes only).
    """
    if type not in _ATOM_TYPES:
        return {"error": f"type must be one of atom, fact, active, got {type!r}"}
    if visibility not in _VISIBILITIES:
        return {"error": f"visibility must be public or private, got {visibility!r}"}
    if type == "active":
        if status not in _ACTIVE_STATUSES:
            return {
                "error": (
                    "type=active requires status in active, someday, blocked, "
                    f"got {status!r}"
                )
            }
        if size not in _ACTIVE_SIZES:
            return {
                "error": (
                    "type=active requires size in small, medium, large, unknown, "
                    f"got {size!r}"
                )
            }
    if edges:
        for edge_type in edges:
            if edge_type not in frontmatter._KNOWN_EDGE_TYPES:
                valid = ", ".join(sorted(frontmatter._KNOWN_EDGE_TYPES))
                return {
                    "error": f"invalid edge type {edge_type!r}: must be one of {valid}"
                }

    with Session(get_engine()) as session:
        note_id = await _index_atom(
            session,
            title=title,
            body=body,
            type=type,
            visibility=visibility,
            tags=tags,
            aliases=aliases,
            edges=edges,
            derived_from_raw=derived_from_raw,
            status=status,
            size=size,
            due=due,
            blocked_by=blocked_by,
        )

        # Close any open gap whose term this atom now defines (research
        # routine + gardener both land here). Same session as the index.
        # NOTE: this stays in create_atom, NOT in _index_atom, because
        # answer_gap drives its own gap-state transition and must not
        # double-resolve through the shared index helper.
        resolve_gaps_for_note(
            session,
            note_id=note_id,
            title=title,
            aliases=aliases or [],
        )

        result: dict = {"note_id": note_id}
        if derived_from_raw is not None:
            raw_row = session.exec(
                select(RawInput).where(RawInput.raw_id == derived_from_raw)
            ).first()
            if raw_row is None:
                result["warning"] = (
                    f"derived_from_raw {derived_from_raw!r} not found, "
                    "provenance not recorded"
                )
            else:
                session.add(
                    AtomRawProvenance(
                        raw_fk=raw_row.id,
                        derived_note_id=note_id,
                        gardener_version=GARDENER_VERSION,
                    )
                )
                session.commit()

    return result


@mcp.tool
async def record_provenance(
    raw_id: str,
    outcome: str,
    error: str | None = None,
) -> dict:
    """Record a sentinel or failure provenance row for a decomposed raw.

    Closes out a raw that the gardener processed but that produced no new
    atoms (no-new-notes), or that failed (failed). A failed outcome
    increments ``retry_count`` on the existing failure row (or inserts one
    with ``retry_count=1``), so the retry ceiling in
    ``raws_needing_decomposition`` is respected.

    Args:
        raw_id: The raw input that was processed.
        outcome: Either no-new-notes or failed.
        error: Optional error detail (only stored for a failed outcome).
    """
    if outcome not in ("no-new-notes", "failed"):
        return {"error": f"outcome must be no-new-notes or failed, got {outcome!r}"}

    with Session(get_engine()) as session:
        row = session.exec(select(RawInput).where(RawInput.raw_id == raw_id)).first()
        if row is None:
            return {"error": f"raw not found: {raw_id}"}

        if outcome == "no-new-notes":
            session.add(
                AtomRawProvenance(
                    raw_fk=row.id,
                    derived_note_id="no-new-notes",
                    gardener_version=GARDENER_VERSION,
                )
            )
        else:
            existing = session.exec(
                select(AtomRawProvenance).where(
                    AtomRawProvenance.raw_fk == row.id,
                    AtomRawProvenance.derived_note_id == "failed",
                )
            ).first()
            if existing is not None:
                existing.retry_count += 1
                existing.error = (error or "")[:500]
                existing.gardener_version = GARDENER_VERSION
                session.add(existing)
            else:
                session.add(
                    AtomRawProvenance(
                        raw_fk=row.id,
                        derived_note_id="failed",
                        gardener_version=GARDENER_VERSION,
                        error=(error or "")[:500],
                        retry_count=1,
                    )
                )
        session.commit()

    return {"recorded": outcome, "raw_id": raw_id}
