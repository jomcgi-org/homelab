"""Shared knowledge-pipeline constants and slug normalization."""

from __future__ import annotations

import re
import unicodedata

from opentelemetry import trace

tracer = trace.get_tracer("knowledge.gardener")

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
    session,
    *,
    apply: bool = False,
    scope: str | None = None,
    max_merges: int | None = None,
    counts: dict[str, int] | None = None,
) -> list[dict]:
    """Plan or atomically merge fact clones; retain every source row.

    Every chunk must match in both directions, and contested facts are skipped.
    The write pass locks candidates before planning for concurrent idempotence.
    max_merges caps clusters in Note.id order, including during dry-run. The
    optional counts output leaves the existing list return value unchanged.
    """
    if max_merges is not None and max_merges < 1:
        raise ValueError("max_merges must be positive")
    if counts is None:
        counts = {}
    counts.clear()
    counts.update(
        merged=0, skipped=0, clusters_found=0, invalidated=0, contested_skipped=0
    )
    with tracer.start_as_current_span("knowledge.merge_clones") as span:
        span.set_attribute("apply", apply)
        if max_merges is not None:
            span.set_attribute("max_merges", max_merges)
        try:
            return _merge_clones(
                session, apply=apply, scope=scope, max_merges=max_merges, counts=counts
            )
        finally:
            # Publish counts even when commit fails. The span context records
            # the exception and ERROR status before ending the run's span.
            span.set_attributes(counts)


def _gardener_clone_clusters(items: list[dict]) -> list[list[dict]]:
    """Vectorise chunk coverage in bounded tiles; recall keeps its scalar path."""
    import numpy as np

    from knowledge.clones import (
        CLONE_COSINE_THRESHOLD,
        _are_clones,
        _clusters_from_pairs,
        cosine,
    )

    scopes: dict[tuple, list[int]] = {}
    for index, item in enumerate(items):
        scopes.setdefault(item.get("scope"), []).append(index)

    def pairs():
        for indexes in scopes.values():
            if len(indexes) < 2:
                continue
            embeddings = [items[i]["embeddings"] for i in indexes]
            vectors = [vector for chunks in embeddings for vector in chunks]
            lengths = {len(vector) if vector is not None else 0 for vector in vectors}
            # The database uses fixed-width embeddings. Preserve the scalar
            # behaviour for malformed, empty, or mixed-width manual inputs.
            if len(lengths) != 1 or 0 in lengths or any(not row for row in embeddings):
                for i, index in enumerate(indexes):
                    for other in indexes[:i]:
                        if _are_clones(items[index], items[other]):
                            yield index, other
                continue
            matrix = np.asarray(vectors, dtype=np.float64)
            norms = np.linalg.norm(matrix, axis=1)
            if not np.all(np.isfinite(norms) & (norms > 1e-75) & (norms < 1e75)):
                for i, index in enumerate(indexes):
                    for other in indexes[:i]:
                        if _are_clones(items[index], items[other]):
                            yield index, other
                continue
            matrix /= norms[:, None]
            offsets = np.concatenate(([0], np.cumsum([len(row) for row in embeddings])))
            for start in range(0, len(indexes), 32):
                end = min(start + 32, len(indexes))
                scores = matrix[offsets[start] : offsets[end]] @ matrix.T
                # Floating-point summation differs from cosine near the exact
                # threshold. Re-evaluate boundary cases with the original rule.
                for left, right in np.argwhere(
                    abs(scores - CLONE_COSINE_THRESHOLD) < 1e-12
                ):
                    scores[left, right] = cosine(
                        vectors[offsets[start] + left], vectors[right]
                    )
                matches = scores >= CLONE_COSINE_THRESHOLD
                for i in range(start, end):
                    if i == 0:
                        continue
                    coverage = matches[
                        offsets[i] - offsets[start] : offsets[i + 1] - offsets[start],
                        : offsets[i],
                    ]
                    left_covered = np.logical_or.reduceat(
                        coverage, offsets[:i], axis=1
                    ).all(axis=0)
                    right_covered = np.logical_and.reduceat(
                        coverage.any(axis=0), offsets[:i]
                    )
                    for j in np.flatnonzero(left_covered & right_covered):
                        yield indexes[i], indexes[j]

    return _clusters_from_pairs(items, pairs())


def _merge_clones(session, *, apply, scope, max_merges, counts) -> list[dict]:
    from datetime import datetime, timezone

    from sqlalchemy import or_
    from sqlmodel import select

    from knowledge.models import AtomRawProvenance, Chunk, Note, NoteLink
    from knowledge.store import DEPLOYMENT_OBSERVATION_SOURCE, open_dispute_note_ids

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
            # Observations are identical by construction (same app, versions)
            # and each is an immutable point-in-time record: merging clones
            # would invalidate history. NULL-safe, a plain != drops NULL source.
            or_(
                Note.source.is_(None),
                Note.source != DEPLOYMENT_OBSERVATION_SOURCE,
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
    counts["contested_skipped"] = len(contested)
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
    additions = []
    groups = [group for group in _gardener_clone_clusters(candidates) if len(group) > 1]
    counts["clusters_found"] = len(groups)
    counts["skipped"] = (
        max(0, len(groups) - max_merges) if max_merges is not None else 0
    )
    for group in groups[:max_merges]:
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
        counts["merged"] += 1
        counts["invalidated"] += len(losers)
        if not apply:
            continue
        merged = list((survivor.extra or {}).get("merged_note_ids", []))
        raw_fks = set(
            session.exec(
                select(AtomRawProvenance.raw_fk).where(
                    AtomRawProvenance.atom_fk == survivor.id
                )
            ).all()
        )
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
                if source.raw_fk is None or source.raw_fk in raw_fks:
                    continue
                additions.append(
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
                raw_fks.add(source.raw_fk)
            loser.verification_state = "invalidated"
            loser.valid_until = datetime.now(timezone.utc)
            loser.extra = {**(loser.extra or {}), "merged_into": survivor.note_id}
            additions.append(
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
    if apply:
        session.add_all(additions)
        session.commit()
    return plans
