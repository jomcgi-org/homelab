"""In-pod Typer CLI for the knowledge gardener (ADR 006 Phase 4a).

Replaces the gardener subprocess's Read/Write/Edit on ``/vault/_processed``
with create/edit/search commands grounded in the monolith's own store, so
every atom is created or mutated through one validated, synchronously
indexed path.

Runs INSIDE the monolith pod against ``DATABASE_URL`` (exactly like the
``knowledge-search`` script): each command opens its own engine + session,
embeds via :class:`shared.embedding.EmbeddingClient`, and indexes through
:func:`knowledge.indexing.index_note_from_raw` (chunk + embed + link-extract
+ upsert, committed). Indexing is async, so each command wraps the coroutine
in ``asyncio.run`` (Typer commands are sync).

The write commands DUAL-WRITE: a ``_processed/<slug>.md`` file on disk plus a
synchronous index into Postgres. The disk file is a safety net through Phase
5: the reconciler deletes notes whose file is missing, and Obsidian sync
still reads the files.

Output discipline: stdout carries the machine-usable result (JSON for
search/get, raw markdown for get-raw, the note_id string for the write
commands). All diagnostics and errors go to stderr. Exit codes: 0 ok,
1 not-found/runtime, 2 validation.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
import yaml

from knowledge import frontmatter
from knowledge.gardener import GARDENER_VERSION, _slugify
from knowledge.indexing import index_note_from_raw
from knowledge.models import AtomRawProvenance, RawInput
from knowledge.notes import _serialize_frontmatter
from knowledge.store import KnowledgeStore
from shared.embedding import EmbeddingClient
from sqlmodel import Session, create_engine, select

app = typer.Typer(
    name="knowledge",
    help="In-pod knowledge gardener: search, read, and create/edit atoms.",
)


class AtomType(str, Enum):
    atom = "atom"
    fact = "fact"
    active = "active"


class Visibility(str, Enum):
    public = "public"
    private = "private"


class Status(str, Enum):
    active = "active"
    someday = "someday"
    blocked = "blocked"


class Size(str, Enum):
    small = "small"
    medium = "medium"
    large = "large"
    unknown = "unknown"


def _engine():
    """Open a SQLModel engine from ``DATABASE_URL`` or exit 1 to stderr."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        typer.echo("knowledge: DATABASE_URL not set", err=True)
        raise typer.Exit(1)
    return create_engine(db_url)


def _vault_root() -> Path:
    return Path(os.environ.get("VAULT_ROOT", "/vault"))


def _read_body(body: str) -> str:
    """Resolve a ``--body`` value, reading stdin when it is ``-``."""
    if body == "-":
        return sys.stdin.read()
    return body


def _parse_edges(edge_strs: list[str]) -> dict[str, list[str]]:
    """Parse ``type:target-slug`` edge specs into an ``{type: [targets]}`` map.

    Raises ``ValueError`` (mapped to exit 2 by callers) on a malformed spec
    or an unknown edge type. Targets are de-duplicated, preserving order.
    """
    edges: dict[str, list[str]] = {}
    for spec in edge_strs:
        if ":" not in spec:
            raise ValueError(f"invalid edge {spec!r}: expected 'type:target-slug'")
        edge_type, _, target = spec.partition(":")
        edge_type = edge_type.strip()
        target = target.strip()
        if edge_type not in frontmatter._KNOWN_EDGE_TYPES:
            valid = ", ".join(sorted(frontmatter._KNOWN_EDGE_TYPES))
            raise ValueError(f"invalid edge type {edge_type!r}: must be one of {valid}")
        if not target:
            raise ValueError(f"invalid edge {spec!r}: empty target")
        edges.setdefault(edge_type, [])
        if target not in edges[edge_type]:
            edges[edge_type].append(target)
    return edges


def _edges_or_exit(edge_strs: list[str]) -> dict[str, list[str]]:
    try:
        return _parse_edges(edge_strs)
    except ValueError as exc:
        typer.echo(f"knowledge: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Natural-language search query")],
    limit: Annotated[int, typer.Option("--limit", help="Max results")] = 5,
) -> None:
    """Semantic search over the notes graph; prints a JSON array to stdout."""
    engine = _engine()
    embed_client = EmbeddingClient()
    embedding = asyncio.run(embed_client.embed(query))
    with Session(engine) as session:
        results = KnowledgeStore(session).search_notes(
            query_embedding=embedding, limit=limit
        )
    # Drop NaN/inf scores so the JSON stays valid and downstream-parseable
    # (mirrors knowledge-search's finite-score filter).
    safe = [
        r
        for r in results
        if isinstance(r.get("score"), (int, float)) and math.isfinite(r["score"])
    ]
    typer.echo(json.dumps(safe))


@app.command()
def get(
    note_id: Annotated[str, typer.Argument(help="Stable note_id")],
) -> None:
    """Print a note's metadata, body, and edges as JSON to stdout."""
    engine = _engine()
    with Session(engine) as session:
        store = KnowledgeStore(session)
        note = store.get_note_by_id(note_id)
        if note is None:
            typer.echo(f"knowledge: note not found: {note_id}", err=True)
            raise typer.Exit(1)
        edges = store.get_note_links(note_id)
    out = {
        "note_id": note["note_id"],
        "title": note["title"],
        "type": note.get("type"),
        "tags": note.get("tags", []),
        "content": note.get("content"),
        "edges": edges,
    }
    typer.echo(json.dumps(out))


@app.command(name="get-raw")
def get_raw(
    raw_id: Annotated[str, typer.Argument(help="raw_inputs.raw_id")],
) -> None:
    """Print a raw input's markdown content (not JSON) to stdout."""
    engine = _engine()
    with Session(engine) as session:
        row = session.exec(select(RawInput).where(RawInput.raw_id == raw_id)).first()
    if row is None:
        typer.echo(f"knowledge: raw not found: {raw_id}", err=True)
        raise typer.Exit(1)
    typer.echo(row.content)


@app.command(name="create-atom")
def create_atom(
    title: Annotated[str, typer.Option("--title", help="Note title (required)")],
    body: Annotated[
        str, typer.Option("--body", help="Body markdown, or '-' to read stdin")
    ],
    note_type: Annotated[AtomType, typer.Option("--type", help="atom | fact | active")],
    visibility: Annotated[
        Visibility, typer.Option("--visibility", help="public | private")
    ],
    tags: Annotated[list[str], typer.Option("--tags", help="Tag (repeatable)")] = [],
    aliases: Annotated[
        list[str], typer.Option("--aliases", help="Alias (repeatable)")
    ] = [],
    edge: Annotated[
        list[str],
        typer.Option("--edge", help="Typed edge 'type:target-slug' (repeatable)"),
    ] = [],
    derived_from_raw: Annotated[
        Optional[str],
        typer.Option("--derived-from-raw", help="raw_id for provenance"),
    ] = None,
    status: Annotated[
        Optional[Status],
        typer.Option("--status", help="active-only: active | someday | blocked"),
    ] = None,
    size: Annotated[
        Optional[Size],
        typer.Option("--size", help="active-only: small | medium | large | unknown"),
    ] = None,
    due: Annotated[
        Optional[str], typer.Option("--due", help="active-only: ISO due date")
    ] = None,
    blocked_by: Annotated[
        list[str],
        typer.Option("--blocked-by", help="active-only: blocking note_id (repeatable)"),
    ] = [],
) -> None:
    """Create a new atom: validate, dual-write the file, and index it.

    On success prints the resolved note_id to stdout. Validation failures
    (bad edge, active without status/size) print a correctable message to
    stderr and exit 2.
    """
    # Schema enforcement before any side effects. type/visibility are
    # required by Typer; active notes additionally require status AND size.
    if note_type is AtomType.active and (status is None or size is None):
        typer.echo(
            "knowledge: type=active requires --status and --size",
            err=True,
        )
        raise typer.Exit(2)
    edges = _edges_or_exit(edge)

    # Resolve the filename collision under _processed/ the same way
    # router.create_note does; the resolved stem becomes the note_id.
    vault_root = _vault_root()
    processed = vault_root / "_processed"
    slug = _slugify(title)
    filename = f"{slug}.md"
    dest = processed / filename
    counter = 1
    while dest.exists():
        filename = f"{slug}-{counter}.md"
        dest = processed / filename
        counter += 1
    note_id = dest.stem
    rel_path = f"_processed/{filename}"

    fm_dict: dict = {
        "id": note_id,
        "title": title,
        "type": note_type.value,
        "visibility": visibility.value,
    }
    if derived_from_raw is not None:
        fm_dict["derived_from_raw"] = derived_from_raw
    if tags:
        fm_dict["tags"] = list(tags)
    if aliases:
        fm_dict["aliases"] = list(aliases)
    if edges:
        fm_dict["edges"] = edges
    if note_type is AtomType.active:
        fm_dict["status"] = status.value
        fm_dict["size"] = size.value
        if due is not None:
            fm_dict["due"] = due
        if blocked_by:
            fm_dict["blocked_by"] = list(blocked_by)

    fm_str = yaml.dump(fm_dict, default_flow_style=False, sort_keys=False)
    file_content = f"---\n{fm_str}---\n\n{_read_body(body).strip()}\n"

    processed.mkdir(parents=True, exist_ok=True)
    dest.write_text(file_content)

    engine = _engine()
    embed_client = EmbeddingClient()
    with Session(engine) as session:
        store = KnowledgeStore(session)
        asyncio.run(
            index_note_from_raw(
                store,
                embed_client,
                note_id=note_id,
                rel_path=rel_path,
                raw=file_content,
            )
        )
        if derived_from_raw is not None:
            raw_row = session.exec(
                select(RawInput).where(RawInput.raw_id == derived_from_raw)
            ).first()
            if raw_row is None:
                typer.echo(
                    f"knowledge: warning: derived-from-raw {derived_from_raw!r} "
                    "not found, skipping provenance",
                    err=True,
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

    typer.echo(note_id)


def _apply_edit(
    note_id: str,
    *,
    body: str | None,
    title: str | None,
    tags: list[str],
    visibility: Visibility | None,
    edge_strs: list[str],
) -> str:
    """Shared load + merge + dual-write + re-index path for edit/patch-edges.

    Reads the existing ``_processed`` file (the note's path from the store),
    merges only the provided fields into the parsed frontmatter (preserving
    aliases, edges, created/updated, and any extras), unions in new edges,
    re-serializes, writes the file, and re-indexes under the existing id.
    Returns the note_id on success.
    """
    new_edges = _edges_or_exit(edge_strs)
    engine = _engine()
    embed_client = EmbeddingClient()
    vault_root = _vault_root()
    with Session(engine) as session:
        store = KnowledgeStore(session)
        note = store.get_note_by_id(note_id)
        if note is None:
            typer.echo(f"knowledge: note not found: {note_id}", err=True)
            raise typer.Exit(1)

        rel_path = note["path"]
        resolved = vault_root / rel_path
        parsed, note_body = frontmatter.parse(resolved.read_text())

        if title is not None:
            parsed.title = title
        if tags:
            parsed.tags = list(tags)
        if visibility is not None:
            parsed.visibility = visibility.value
        if body is not None:
            note_body = _read_body(body).strip()

        # Union new edges into the existing edges map, preserving order.
        for edge_type, targets in new_edges.items():
            existing = parsed.edges.setdefault(edge_type, [])
            for target in targets:
                if target not in existing:
                    existing.append(target)

        file_content = _serialize_frontmatter(parsed, note_body)
        resolved.write_text(file_content)

        existing_id = note["note_id"]
        asyncio.run(
            index_note_from_raw(
                store,
                embed_client,
                note_id=existing_id,
                rel_path=rel_path,
                raw=file_content,
            )
        )
    return existing_id


@app.command()
def edit(
    note_id: Annotated[str, typer.Argument(help="Stable note_id")],
    body: Annotated[
        Optional[str], typer.Option("--body", help="New body, or '-' to read stdin")
    ] = None,
    title: Annotated[Optional[str], typer.Option("--title", help="New title")] = None,
    tags: Annotated[
        list[str], typer.Option("--tags", help="Replace tags (repeatable)")
    ] = [],
    visibility: Annotated[
        Optional[Visibility], typer.Option("--visibility", help="public | private")
    ] = None,
    edge: Annotated[
        list[str],
        typer.Option(
            "--edge", "--add-edge", help="Merge edge 'type:target-slug' (repeatable)"
        ),
    ] = [],
) -> None:
    """Edit a note's frontmatter/body in place, then re-index. Prints note_id."""
    result_id = _apply_edit(
        note_id,
        body=body,
        title=title,
        tags=tags,
        visibility=visibility,
        edge_strs=edge,
    )
    typer.echo(result_id)


@app.command(name="patch-edges")
def patch_edges(
    note_id: Annotated[str, typer.Argument(help="Stable note_id")],
    edge: Annotated[
        list[str],
        typer.Option("--edge", help="Add edge 'type:target-slug' (repeatable)"),
    ] = [],
) -> None:
    """Add typed edges to a note (union with existing), re-index. Prints note_id."""
    result_id = _apply_edit(
        note_id,
        body=None,
        title=None,
        tags=[],
        visibility=None,
        edge_strs=edge,
    )
    typer.echo(result_id)


if __name__ == "__main__":
    app()
