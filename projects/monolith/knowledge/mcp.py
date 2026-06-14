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
from sqlmodel import Session

from app.db import get_engine
from app.mcp_app import mcp
from knowledge import frontmatter
from knowledge import notes as notes_module
from knowledge.gaps import answer_gap as _answer_gap
from knowledge.gaps import approve_gap as _approve_gap
from knowledge.gaps import list_review_queue, split_csv
from knowledge.gardener import _slugify
from knowledge.indexing import index_note_best_effort
from knowledge.notes import resolve_note_body
from knowledge.service import DEFAULT_VAULT_ROOT, VAULT_ROOT_ENV
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
    """Answer an in-review gap, emitting a personal-tier atom in the vault.

    Writes a new markdown file under ``<vault>/_processed/`` with
    ``source_tier: personal`` and marks the gap as committed.

    Args:
        gap_id: The id of a gap currently in ``state='in_review'``.
        answer: The user's answer text. May not contain a frontmatter
            terminator (a line containing only ``---``).
    """
    vault_root = Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT)).resolve()
    with Session(get_engine()) as session:
        try:
            return _answer_gap(session, gap_id, answer, vault_root)
        except ValueError as exc:
            return {"error": str(exc)}


@mcp.tool
async def approve_research_gap(gap_id: int) -> dict:
    """Approve an external gap for auto-research.

    Use this from the pending review queue when an external gap is worth
    Sonnet web-research tokens. Internal and hybrid gaps must be answered
    via answer_gap instead, approval rejects them.

    Args:
        gap_id: The id of a gap currently in state in_review with
            gap_class external.
    """
    vault_root = Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT)).resolve()
    with Session(get_engine()) as session:
        try:
            return _approve_gap(session, gap_id, vault_root)
        except ValueError as exc:
            return {"error": str(exc)}
