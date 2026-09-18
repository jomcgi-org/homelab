"""Shared knowledge-pipeline constants and slug normalization."""

from __future__ import annotations

import re
import unicodedata

# Version stamp recorded on every provenance row the gardener produces.
# Bump this when the prompt or model changes to trigger a manual reprocess.
GARDENER_VERSION = "claude-sonnet-4-6@v1"

MAX_GARDENER_RETRIES = 3

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text_in: str) -> str:
    normalized = unicodedata.normalize("NFKD", text_in)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_only.lower()).strip("-")
    return slug or "note"


def merge_clones(
    session, *, apply: bool = False, scope: str | None = None
) -> list[dict]:
    """Plan or atomically merge fact clones; retain every source row.

    Every chunk must match in both directions, and contested facts are skipped.
    The write pass locks candidates before planning for concurrent idempotence.
    """
    from datetime import datetime, timezone

    from sqlalchemy import or_
    from sqlmodel import select

    from knowledge.clones import clone_clusters
    from knowledge.models import AtomRawProvenance, Chunk, Note, NoteLink
    from knowledge.store import open_dispute_note_ids

    statement = (
        select(Note)
        .where(
            Note.type == "fact",
            Note.deleted_at.is_(None),
            Note.verification_state.notin_(["invalidated", "disputed"]),
            or_(
                Note.valid_until.is_(None),
                Note.valid_until > datetime.now(timezone.utc),
            ),
        )
        .order_by(Note.id)
    )
    if scope is not None:
        statement = statement.where(Note.scope == scope)
    if apply:
        statement = statement.with_for_update()
    notes = session.exec(statement).all()
    contested = open_dispute_note_ids(session, [note.note_id for note in notes])
    chunks: dict[int, list] = {}
    for chunk in session.exec(
        select(Chunk).where(Chunk.note_fk.in_([note.id for note in notes]))
    ):
        chunks.setdefault(chunk.note_fk, []).append(chunk)
    candidates = [
        {
            "note_id": note.note_id,
            "scope": (note.scope, note.visibility),
            "verification_state": note.verification_state,
            "confidence": note.confidence,
            "embeddings": [chunk.embedding for chunk in chunks[note.id]],
            "row": note,
        }
        for note in notes
        if chunks.get(note.id) and note.note_id not in contested
    ]
    plans = []
    for group in clone_clusters(candidates):
        if len(group) < 2:
            continue
        survivor = group[0]["row"]
        losers = [item["row"] for item in group[1:]]
        plans.append(
            {
                "survivor": survivor.note_id,
                "invalidated": [note.note_id for note in losers],
            }
        )
        if not apply:
            continue
        merged = list((survivor.extra or {}).get("merged_note_ids", []))
        for loser in losers:
            # Copy provenance to the survivor without destroying the original
            # extraction record, timestamps, errors, or gardener version.
            sources = session.exec(
                select(AtomRawProvenance).where(
                    or_(
                        AtomRawProvenance.atom_fk == loser.id,
                        AtomRawProvenance.derived_note_id == loser.note_id,
                    )
                )
            ).all()
            for source in sources:
                session.add(
                    AtomRawProvenance(
                        atom_fk=survivor.id,
                        raw_fk=source.raw_fk,
                        derived_note_id=survivor.note_id,
                        gardener_version=source.gardener_version,
                        created_at=source.created_at,
                        error=source.error,
                        retry_count=source.retry_count,
                    )
                )
            loser.verification_state = "invalidated"
            loser.valid_until = datetime.now(timezone.utc)
            loser.extra = {**(loser.extra or {}), "merged_into": survivor.note_id}
            session.add(loser)
            session.add(
                NoteLink(
                    src_note_fk=survivor.id,
                    target_id=loser.note_id,
                    kind="edge",
                    edge_type="supersedes",
                )
            )
            merged.append(loser.note_id)
            merged.extend((loser.extra or {}).get("merged_note_ids", []))
        survivor.extra = {
            **(survivor.extra or {}),
            "merged_note_ids": sorted(set(merged)),
        }
        session.add(survivor)
    if apply:
        session.commit()
    return plans
