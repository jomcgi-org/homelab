"""Postgres data access layer for the knowledge schema."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, not_, or_
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, delete, select

from knowledge.frontmatter import ParsedFrontmatter
from knowledge.gardener import GARDENER_VERSION, MAX_GARDENER_RETRIES, _slugify
from knowledge.links import Link
from knowledge.models import AtomRawProvenance, Chunk, Gap, Note, NoteLink, RawInput
from knowledge.chunker import Chunk as ChunkPayload

logger = logging.getLogger(__name__)

# Minimum adjusted score to include in search results.  The adjusted
# score is ``(1 - cosine_distance) * min(1, chunk_len / 100)`` — a
# chunk-length penalty that downranks ultra-short stubs whose embeddings
# are too generic to be meaningful.
MIN_SEARCH_SCORE = 0.4
_CHUNK_LEN_RAMP = 100  # chars at which the length penalty reaches 1.0

# Terminal task statuses. A task in one of these is finished and should drop
# out of the daily/weekly "what's on my plate" views (and the MCP tools that
# back them). patch_task also uses this set to stamp/clear task-completed on
# transition, so keeping one definition avoids the two drifting apart.
_DONE_STATUSES = frozenset({"done", "cancelled"})

# Note types that participate in the knowledge graph. Notes whose type
# is NULL or not in this set are dropped from the graph response — they
# are usually pre-pipeline imports that haven't been classified yet,
# with no useful colour or label affordance. This filter used to live
# client-side in +page.svelte; moving it here makes the server the
# single source of truth so degree counts and edges are coherent with
# what the user sees.
GRAPH_NOTE_TYPES = frozenset({"atom", "fact", "raw", "gap", "active", "paper"})


def _resolve_edge_targets(session: Session, rows: list) -> set[str]:
    """Return the set of target_id values (for kind='edge' rows) that exist as notes."""
    edge_ids = {r.target_id for r in rows if r.kind == "edge"}
    if not edge_ids:
        return set()
    return set(
        session.execute(select(Note.note_id).where(Note.note_id.in_(edge_ids)))
        .scalars()
        .all()
    )


class KnowledgeStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_indexed(self) -> dict[str, str]:
        # Soft-deleted rows are excluded so the reconciler doesn't see a
        # note's _trash/ path as "indexed" and then hard-delete the row
        # when _walk() reports the file is missing from _processed/ /
        # _researching/. Without this filter the soft-delete window would
        # silently collapse to one reconciler cycle (~5 min).
        result = self.session.execute(
            select(Note.path, Note.content_hash).where(Note.deleted_at.is_(None))
        )
        return {path: ch for path, ch in result.all()}

    def notes_missing_content(self) -> list[Note]:
        """Live notes whose ``content`` column has not been populated yet.

        Drives the ADR 006 one-shot backfill: pre-existing rows predate the
        ``content`` column and carry NULL until the reconciler reads their
        body from disk and fills it in. Returns tracked ORM rows so the
        caller can mutate ``content`` and commit once (no per-row commit,
        no re-embedding). After the corpus is backfilled this returns ``[]``.
        """
        return list(
            self.session.execute(
                select(Note).where(Note.content.is_(None), Note.deleted_at.is_(None))
            )
            .scalars()
            .all()
        )

    def upsert_note(
        self,
        *,
        note_id: str,
        path: str,
        content_hash: str,
        title: str,
        metadata: ParsedFrontmatter,
        chunks: list[ChunkPayload],
        vectors: list[list[float]],
        links: list[Link],
        content: str | None = None,
    ) -> None:
        # Wrap delete+insert in a SAVEPOINT so a mid-insert failure rolls
        # back the cascade deletes — the existing row is preserved even if
        # the caller never rolls back explicitly.
        with self.session.begin_nested():
            # Delete existing note and its dependents. Explicit cascade for
            # portability across Postgres and SQLite (which needs PRAGMA
            # foreign_keys=ON for FK cascades to fire).
            # Fall back to note_id lookup to handle path migrations (e.g.
            # notes moved from their original vault path into _processed/).
            existing = self.session.execute(
                select(Note.id).where(Note.path == path)
            ).scalar_one_or_none()
            if existing is None:
                existing = self.session.execute(
                    select(Note.id).where(Note.note_id == note_id)
                ).scalar_one_or_none()
            if existing is not None:
                self.session.execute(delete(Chunk).where(Chunk.note_fk == existing))
                self.session.execute(
                    delete(NoteLink).where(NoteLink.src_note_fk == existing)
                )
                self.session.execute(delete(Note).where(Note.id == existing))
                self.session.flush()

            note = Note(
                note_id=note_id,
                path=path,
                title=title,
                content_hash=content_hash,
                content=content,
                type=metadata.type,
                status=metadata.status,
                visibility=metadata.visibility,
                source=metadata.source,
                tags=metadata.tags,
                aliases=metadata.aliases,
                created_at=metadata.created,
                updated_at=metadata.updated,
                extra=metadata.extra,
                indexed_at=datetime.now(timezone.utc),
            )
            self.session.add(note)
            self.session.flush()

            for chunk, vector in zip(chunks, vectors, strict=True):
                self.session.add(
                    Chunk(
                        note_fk=note.id,
                        chunk_index=chunk["index"],
                        section_header=chunk["section_header"],
                        chunk_text=chunk["text"],
                        embedding=vector,
                    )
                )

            # Untyped body wikilinks -> kind='link'.
            for link in links:
                self.session.add(
                    NoteLink(
                        src_note_fk=note.id,
                        target_id=link.target,
                        target_title=link.display,
                        kind="link",
                        edge_type=None,
                    )
                )

            # Typed frontmatter edges -> kind='edge', edge_type=<key>.
            for edge_type, targets in metadata.edges.items():
                for target in targets:
                    self.session.add(
                        NoteLink(
                            src_note_fk=note.id,
                            target_id=target,
                            target_title=None,
                            kind="edge",
                            edge_type=edge_type,
                        )
                    )
            self.session.flush()

        self.session.commit()

    def search_notes(
        self,
        query_embedding: list[float],
        limit: int = 5,
        exclude_ids: list[str] | None = None,
    ) -> list[dict]:
        """Semantic search over notes using cosine similarity.

        Joins Note with Chunk, computes pgvector cosine_distance on
        Chunk.embedding, groups by note, and returns the best (minimum
        distance) chunk score per note as ``score = 1 - distance``,
        penalised by chunk length to downrank ultra-short stubs.

        Soft-deleted notes (``deleted_at IS NOT NULL``) are excluded —
        their content should not surface in search after a delete.
        """
        distance = Chunk.embedding.cosine_distance(query_embedding)
        len_penalty = func.least(
            1.0,
            func.length(Chunk.chunk_text) / float(_CHUNK_LEN_RAMP),
        )
        adjusted = (1 - distance) * len_penalty
        best_score = func.max(adjusted).label("score")

        stmt = (
            select(
                Note.note_id,
                Note.title,
                Note.path,
                best_score,
            )
            .join(Chunk, Chunk.note_fk == Note.id)
            .where(Note.deleted_at.is_(None))
            .group_by(Note.id)
            .having(func.max(adjusted) >= MIN_SEARCH_SCORE)
            .order_by(best_score.desc())
            .limit(limit)
        )

        if exclude_ids:
            stmt = stmt.where(Note.note_id.notin_(exclude_ids))

        rows = self.session.execute(stmt).all()
        return [
            {
                "note_id": row.note_id,
                "title": row.title,
                "path": row.path,
                "score": float(row.score),
            }
            for row in rows
        ]

    def search_notes_with_context(
        self,
        query_embedding: list[float],
        limit: int = 20,
        type_filter: str | None = None,
    ) -> list[dict]:
        """Semantic search returning type, tags, best chunk section + snippet.

        Powers the knowledge search overlay (ADR 003). Runs two SQL
        round-trips:

        1. Top-N notes ranked by ``best_score = 1 - min(cosine_distance)``
           across their chunks, with optional ``Note.type`` filter.
        2. A single batched ``SELECT DISTINCT ON (note_fk)`` to pick the
           best-matching chunk per top-N note — no N+1.

        Results are stitched in Python into dicts with keys:
        ``note_id, title, path, type, tags, score, section, snippet``.
        """
        distance = Chunk.embedding.cosine_distance(query_embedding)
        len_penalty = func.least(
            1.0,
            func.length(Chunk.chunk_text) / float(_CHUNK_LEN_RAMP),
        )
        adjusted = (1 - distance) * len_penalty
        best_score = func.max(adjusted).label("score")

        notes_stmt = (
            select(
                Note.id,
                Note.note_id,
                Note.title,
                Note.path,
                Note.type,
                Note.tags,
                best_score,
            )
            .join(Chunk, Chunk.note_fk == Note.id)
            .where(Note.deleted_at.is_(None))
            .group_by(Note.id)
            .having(func.max(adjusted) >= MIN_SEARCH_SCORE)
            .order_by(best_score.desc())
            .limit(limit)
        )
        if type_filter is not None:
            notes_stmt = notes_stmt.where(Note.type == type_filter)

        note_rows = self.session.execute(notes_stmt).all()
        if not note_rows:
            return []

        top_ids = [row.id for row in note_rows]

        chunk_adj = (1 - Chunk.embedding.cosine_distance(query_embedding)) * func.least(
            1.0, func.length(Chunk.chunk_text) / float(_CHUNK_LEN_RAMP)
        )
        chunks_stmt = (
            select(
                Chunk.note_fk,
                Chunk.section_header,
                Chunk.chunk_text,
            )
            .where(Chunk.note_fk.in_(top_ids))
            .order_by(Chunk.note_fk, chunk_adj.desc())
            .distinct(Chunk.note_fk)
        )

        chunk_rows = self.session.execute(chunks_stmt).all()
        best_chunk_by_note = {
            row.note_fk: (row.section_header, row.chunk_text) for row in chunk_rows
        }

        # 3. Batch-fetch edges for top-N notes.
        edge_rows = self.session.execute(
            select(
                NoteLink.src_note_fk,
                NoteLink.target_id,
                NoteLink.target_title,
                NoteLink.kind,
                NoteLink.edge_type,
            ).where(NoteLink.src_note_fk.in_(top_ids))
        ).all()

        # Resolve typed edges: check which target note_ids exist so
        # consumers know which edges are navigable.
        resolved = _resolve_edge_targets(self.session, edge_rows)

        edges_by_note: dict[int, list[dict]] = {}
        for row in edge_rows:
            edge: dict = {
                "target_id": row.target_id,
                "target_title": row.target_title,
                "kind": row.kind,
                "edge_type": row.edge_type,
            }
            if row.kind == "edge":
                edge["resolved_note_id"] = (
                    row.target_id if row.target_id in resolved else None
                )
            edges_by_note.setdefault(row.src_note_fk, []).append(edge)

        results: list[dict] = []
        for row in note_rows:
            section, chunk_text = best_chunk_by_note.get(row.id, ("", ""))
            results.append(
                {
                    "note_id": row.note_id,
                    "title": row.title,
                    "path": row.path,
                    "type": row.type,
                    "tags": list(row.tags or []),
                    "score": float(row.score),
                    "section": section,
                    "snippet": (chunk_text or "")[:240],
                    "edges": edges_by_note.get(row.id, []),
                }
            )
        return results

    def get_note_by_id(self, note_id: str) -> dict | None:
        """Fetch note metadata plus its body by stable ``note_id``.

        Returns ``None`` if no note matches. Includes the authoritative
        ``content`` (ADR 006) so note-detail readers can serve the body
        from Postgres; ``content`` is ``None`` for rows whose Phase 1
        backfill has not run yet, in which case callers fall back to the
        vault file. Used by the note-detail and MCP ``get_note`` paths.
        """
        row = self.session.execute(
            select(
                Note.note_id,
                Note.title,
                Note.path,
                Note.type,
                Note.tags,
                Note.content,
            )
            .where(Note.note_id == note_id)
            .where(Note.deleted_at.is_(None))
            .limit(1)
        ).first()
        if row is None:
            return None
        return {
            "note_id": row.note_id,
            "title": row.title,
            "path": row.path,
            "type": row.type,
            "tags": list(row.tags or []),
            "content": row.content,
        }

    def get_graph(self) -> dict:
        """Return the full knowledge graph: nodes (notes) and edges (links).

        Notes whose ``type`` is NULL or not in :data:`GRAPH_NOTE_TYPES`
        are dropped from the response. This filter used to live in the
        Svelte client; moving it here makes the server the single source
        of truth so server-computed ``degree`` matches the visible-edge
        count the user sees.

        Edges with unresolved targets are dropped — no phantom edges.
        Targets are resolved by slugifying ``NoteLink.target_id`` (which
        stores the raw user-typed wikilink text, e.g. ``"Steve Krug"``)
        and matching against the slugified ``note_id`` of each visible
        note. This is the same normalization ``gaps.discover_gaps`` uses
        when promoting unresolved wikilinks into ``type='gap'`` stubs, so
        a wikilink ``[[Steve Krug]]`` consistently resolves to the gap at
        ``_researching/steve-krug.md`` instead of being treated as an
        unresolved string. The edge's ``target`` field is rewritten to
        the canonical ``note_id`` so it joins cleanly with the
        corresponding node on the frontend.

        Each node payload also ships server-computed ``degree`` (count of
        incident visible edges) and persisted ``x`` / ``y`` layout
        positions (``None`` until a layout job has run).
        """
        all_note_rows = self.session.execute(
            select(
                Note.note_id,
                Note.title,
                Note.type,
                Note.indexed_at,
                Note.layout_x,
                Note.layout_y,
            ).where(Note.deleted_at.is_(None))
        ).all()
        # Filter out notes whose type isn't in the graph set BEFORE we
        # build note_ids — that way the edge filter automatically drops
        # edges that point at filtered-out neighbours, keeping degree
        # counts coherent with the visible edges.
        note_rows = [row for row in all_note_rows if row.type in GRAPH_NOTE_TYPES]
        note_ids = {row.note_id for row in note_rows}
        # Map each visible note's slug back to its canonical note_id so a
        # title-form wikilink (``"Steve Krug"``) resolves to the same node
        # as its slug-form counterpart (``"steve-krug"``).
        slug_to_note_id = {_slugify(nid): nid for nid in note_ids}

        link_rows = self.session.execute(
            select(
                Note.note_id.label("source"),
                NoteLink.target_id.label("target"),
                NoteLink.kind,
                NoteLink.edge_type,
            )
            .join(Note, NoteLink.src_note_fk == Note.id)
            .where(Note.deleted_at.is_(None))
        ).all()

        edges: list[dict] = []
        for row in link_rows:
            if row.source not in note_ids:
                continue
            canonical_target = slug_to_note_id.get(_slugify(row.target))
            if canonical_target is None:
                continue
            edges.append(
                {
                    "source": row.source,
                    "target": canonical_target,
                    "kind": row.kind,
                    "edge_type": row.edge_type,
                }
            )

        # Compute degree from the response edges so server-side semantics
        # match exactly what the client used to compute (increment both
        # endpoints of each visible edge).
        degree_by_note_id: dict[str, int] = {}
        for edge in edges:
            degree_by_note_id[edge["source"]] = (
                degree_by_note_id.get(edge["source"], 0) + 1
            )
            degree_by_note_id[edge["target"]] = (
                degree_by_note_id.get(edge["target"], 0) + 1
            )

        # Orphans (no edges) carry no graph information; we filter them
        # out so the layout step doesn't waste effort and the renderer
        # doesn't show floating dots. Most orphans are legacy vault-era
        # type='gap' stub notes: the fileless discover_gaps no longer
        # creates stub notes (gaps live in knowledge.gaps now), so the
        # pre-migration stubs whose term is no longer wikilinked anywhere
        # have nothing pointing at them. Filtering them here is correct;
        # purging the vestigial stubs is tracked separately.
        connected_note_ids = set(degree_by_note_id)
        connected_note_rows = [
            row for row in note_rows if row.note_id in connected_note_ids
        ]

        return {
            "nodes": [
                {
                    "id": row.note_id,
                    "title": row.title,
                    "type": row.type,
                    "degree": degree_by_note_id.get(row.note_id, 0),
                    "x": row.layout_x,
                    "y": row.layout_y,
                }
                for row in connected_note_rows
            ],
            "edges": edges,
            # indexed_at remains the cluster-wide max — it represents the
            # last time *any* note (visible or not) was indexed, which is
            # what the cache headers want.
            "indexed_at": max((row.indexed_at for row in all_note_rows), default=None),
        }

    def get_note_links(self, note_id: str) -> list[dict]:
        """Fetch all outgoing links/edges for a note by its stable note_id."""
        note_fk = self.session.execute(
            select(Note.id).where(Note.note_id == note_id)
        ).scalar_one_or_none()
        if note_fk is None:
            return []
        rows = self.session.execute(
            select(
                NoteLink.target_id,
                NoteLink.target_title,
                NoteLink.kind,
                NoteLink.edge_type,
            ).where(NoteLink.src_note_fk == note_fk)
        ).all()

        resolved = _resolve_edge_targets(self.session, rows)

        results: list[dict] = []
        for row in rows:
            edge: dict = {
                "target_id": row.target_id,
                "target_title": row.target_title,
                "kind": row.kind,
                "edge_type": row.edge_type,
            }
            if row.kind == "edge":
                edge["resolved_note_id"] = (
                    row.target_id if row.target_id in resolved else None
                )
            results.append(edge)
        return results

    def delete_note(self, path: str) -> None:
        existing = self.session.execute(
            select(Note.id).where(Note.path == path)
        ).scalar_one_or_none()
        if existing is None:
            logger.info("knowledge: delete_note called for absent path %s", path)
            return
        self.session.execute(delete(Chunk).where(Chunk.note_fk == existing))
        self.session.execute(delete(NoteLink).where(NoteLink.src_note_fk == existing))
        self.session.execute(delete(Note).where(Note.id == existing))
        self.session.commit()

    def list_tasks(
        self,
        *,
        statuses: list[str] | None = None,
        exclude_statuses: Iterable[str] | None = None,
        due_before: str | None = None,
        due_after: str | None = None,
        sizes: list[str] | None = None,
        include_someday: bool = False,
    ) -> list[dict]:
        """Return task notes (type='active') with optional filters.

        Filtering is done in Python to avoid JSONB dialect differences
        between Postgres and SQLite (used in tests). Task counts are
        always small (< 1000) so this is fine.
        """
        stmt = (
            select(Note)
            .where(Note.type == "active")
            .where(Note.deleted_at.is_(None))
            .order_by(Note.indexed_at.desc())
        )
        notes = self.session.execute(stmt).scalars().all()

        results: list[dict] = []
        for note in notes:
            extra = note.extra or {}
            status = extra.get("status", "")
            due = extra.get("due")
            size = extra.get("size")

            # Exclude someday by default.
            if not include_someday and status == "someday":
                continue

            if statuses is not None and status not in statuses:
                continue

            # Exclude-list runs after the allowlist so callers can pass an
            # empty/blank status through (unlike an allowlist, which would
            # silently drop unknown statuses) while still dropping terminal ones.
            if exclude_statuses is not None and status in exclude_statuses:
                continue

            if due_before is not None and (due is None or due > due_before):
                continue

            if due_after is not None and (due is None or due < due_after):
                continue

            if sizes is not None and size not in sizes:
                continue

            results.append(
                {
                    "note_id": note.note_id,
                    "title": note.title,
                    "tags": list(note.tags or []),
                    "status": status,
                    "due": due,
                    "size": size,
                    "blocked_by": extra.get("blocked-by", []),
                    "task_completed": extra.get("task-completed"),
                }
            )
        return results

    def search_tasks(
        self,
        query_embedding: list[float],
        *,
        statuses: list[str] | None = None,
        include_someday: bool = False,
        limit: int = 20,
    ) -> list[dict]:
        """Semantic search over task notes (type='active').

        Runs a vector search scoped to active notes, then applies
        status filtering in Python (same strategy as list_tasks).
        """
        distance = Chunk.embedding.cosine_distance(query_embedding)
        len_penalty = func.least(
            1.0,
            func.length(Chunk.chunk_text) / float(_CHUNK_LEN_RAMP),
        )
        adjusted = (1 - distance) * len_penalty
        best_score = func.max(adjusted).label("score")

        stmt = (
            select(
                Note.note_id,
                Note.title,
                Note.tags,
                Note.extra,
                best_score,
            )
            .join(Chunk, Chunk.note_fk == Note.id)
            .where(Note.type == "active")
            .where(Note.deleted_at.is_(None))
            .group_by(Note.id)
            .having(func.max(adjusted) >= MIN_SEARCH_SCORE)
            .order_by(best_score.desc())
            .limit(limit)
        )

        rows = self.session.execute(stmt).all()

        results: list[dict] = []
        for row in rows:
            extra = row.extra or {}
            status = extra.get("status", "")

            if not include_someday and status == "someday":
                continue
            if statuses is not None and status not in statuses:
                continue

            results.append(
                {
                    "note_id": row.note_id,
                    "title": row.title,
                    "tags": list(row.tags or []),
                    "status": status,
                    "due": extra.get("due"),
                    "size": extra.get("size"),
                    "blocked_by": extra.get("blocked-by", []),
                    "task_completed": extra.get("task-completed"),
                    "score": float(row.score),
                }
            )
        return results

    def list_tasks_daily(self) -> list[dict]:
        """Tasks due today or overdue, excluding finished (done/cancelled) ones."""
        today = date.today().isoformat()
        return self.list_tasks(
            due_before=today,
            include_someday=False,
            exclude_statuses=_DONE_STATUSES,
        )

    def list_tasks_weekly(self) -> list[dict]:
        """Tasks due this week (Monday through Sunday), excluding finished ones."""
        today = date.today()
        # Find end of current week (Sunday).
        days_until_sunday = 6 - today.weekday()
        end_of_week = (today + timedelta(days=days_until_sunday)).isoformat()
        return self.list_tasks(
            due_before=end_of_week,
            include_someday=False,
            exclude_statuses=_DONE_STATUSES,
        )

    def patch_task(self, note_id: str, fields: dict) -> None:
        """Update JSONB extra fields on a task note.

        Raises ValueError if the note_id does not exist or is not type='active'.
        Auto-sets ``task-completed`` to today when status transitions to
        done/cancelled, and clears it when moving away from those statuses.
        """
        note = self.session.execute(
            select(Note).where(
                Note.note_id == note_id,
                Note.type == "active",
                Note.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if note is None:
            raise ValueError(f"Task not found: {note_id}")

        extra = dict(note.extra or {})

        # Detect status transitions for auto-completing.
        done_statuses = _DONE_STATUSES
        old_status = extra.get("status", "")
        new_status = fields.get("status", old_status)

        extra.update(fields)

        if new_status in done_statuses and old_status not in done_statuses:
            extra["task-completed"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        elif new_status not in done_statuses and old_status in done_statuses:
            extra.pop("task-completed", None)

        note.extra = extra
        flag_modified(note, "extra")
        self.session.commit()

    def list_gaps(
        self,
        *,
        states: list[str] | None = None,
        classes: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """List gaps with optional state/class filters, most recent first.

        Soft-deleted gaps (``deleted_at IS NOT NULL``) are excluded.
        """
        stmt = (
            select(Gap)
            .where(Gap.deleted_at.is_(None))
            .order_by(Gap.created_at.desc(), Gap.id.desc())
            .limit(limit)
        )
        if states:
            stmt = stmt.where(Gap.state.in_(states))
        if classes:
            stmt = stmt.where(Gap.gap_class.in_(classes))

        rows = self.session.execute(stmt).scalars().all()
        return [
            {
                "id": gap.id,
                "term": gap.term,
                "context": gap.context,
                "gap_class": gap.gap_class,
                "state": gap.state,
                "answer": gap.answer,
                "created_at": gap.created_at,
                "classified_at": gap.classified_at,
                "resolved_at": gap.resolved_at,
                "pipeline_version": gap.pipeline_version,
            }
            for gap in rows
        ]

    def get_gap_by_id(self, gap_id: int) -> dict | None:
        """Fetch a single gap by id, or None if no gap matches.

        Soft-deleted gaps return ``None`` so callers can't accidentally
        surface tombstoned rows. Restore via :func:`gaps.undelete_gap`.
        """
        gap = self.session.get(Gap, gap_id)
        if gap is None or gap.deleted_at is not None:
            return None
        return {
            "id": gap.id,
            "term": gap.term,
            "context": gap.context,
            "gap_class": gap.gap_class,
            "state": gap.state,
            "answer": gap.answer,
            "created_at": gap.created_at,
            "classified_at": gap.classified_at,
            "resolved_at": gap.resolved_at,
            "pipeline_version": gap.pipeline_version,
        }

    def raws_needing_decomposition(self, limit: int = 10) -> list[RawInput]:
        """Return raws the gardener still needs to decompose, fresh first.

        Tier 1 (fresh): no current-version or pre-migration provenance at all.
        Tier 2 (retriable): have a ``derived_note_id='failed'`` provenance row
        with ``retry_count < MAX_GARDENER_RETRIES`` and no successful
        current-version provenance.

        Returns ``(fresh + retriable)[:limit]``, preserving tier order.
        """
        # Subquery: raw_fk values that have current-version or pre-migration
        # provenance (i.e. successfully handled or grandfathered).
        handled_subq = (
            select(AtomRawProvenance.raw_fk)
            .where(AtomRawProvenance.raw_fk.is_not(None))
            .where(
                or_(
                    AtomRawProvenance.gardener_version == GARDENER_VERSION,
                    AtomRawProvenance.gardener_version == "pre-migration",
                )
            )
            .where(
                or_(
                    AtomRawProvenance.derived_note_id.is_(None),
                    AtomRawProvenance.derived_note_id != "failed",
                )
            )
            .subquery()
        )

        # Subquery: raw_fk values that have a "failed" provenance row
        # (regardless of retry_count: filtered in tier 2).
        failed_subq = (
            select(AtomRawProvenance.raw_fk)
            .where(AtomRawProvenance.raw_fk.is_not(None))
            .where(AtomRawProvenance.derived_note_id == "failed")
            .subquery()
        )

        # Tier 1: fresh raws, not handled AND not failed.
        fresh_stmt = (
            select(RawInput)
            .where(not_(RawInput.id.in_(select(handled_subq.c.raw_fk))))
            .where(not_(RawInput.id.in_(select(failed_subq.c.raw_fk))))
            .order_by(RawInput.created_at.asc().nullslast(), RawInput.id.asc())
        )
        fresh = list(self.session.exec(fresh_stmt).all())

        # Tier 2: retriable failed raws have a "failed" row with
        # retry_count < _MAX_RETRIES and no successful current-version prov.
        retriable_subq = (
            select(AtomRawProvenance.raw_fk)
            .where(AtomRawProvenance.raw_fk.is_not(None))
            .where(AtomRawProvenance.derived_note_id == "failed")
            .where(AtomRawProvenance.retry_count < MAX_GARDENER_RETRIES)
            .subquery()
        )
        retriable_stmt = (
            select(RawInput)
            .where(RawInput.id.in_(select(retriable_subq.c.raw_fk)))
            .where(not_(RawInput.id.in_(select(handled_subq.c.raw_fk))))
            .order_by(RawInput.created_at.asc().nullslast(), RawInput.id.asc())
        )
        retriable = list(self.session.exec(retriable_stmt).all())

        return (fresh + retriable)[:limit]
