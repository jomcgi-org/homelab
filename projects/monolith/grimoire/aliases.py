"""Review-first alias candidates and transactional Grimoire entity merges."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from sqlalchemy import or_
from sqlmodel import Session, select

from grimoire.extract import _canonicalize_name
from grimoire.models import (
    ENTITY_DETAIL_MODELS,
    ChunkEntityMention,
    Embedding,
    Entity,
    EntityAliasReview,
    EntityVerification,
    EntityVerificationRetry,
    KnowledgeChunk,
    KnowledgeGrant,
    Relationship,
)


class AliasMergeError(RuntimeError):
    """An approved pair could not be merged without violating invariants."""


MERGE_RETRY_BASE_SECONDS = 60
MERGE_RETRY_MAX_SECONDS = 3600


class Embedder(Protocol):
    model: str

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


def _name_tokens(name: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[\w']+", _canonicalize_name(name).casefold()))


def _is_name_pair(first: Entity, second: Entity) -> bool:
    a = _name_tokens(first.name)
    b = _name_tokens(second.name)
    if not a or not b or a == b:
        return False
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return long[: len(short)] == short or set(short) < set(long)


def _survivor_and_twin(first: Entity, second: Entity) -> tuple[Entity, Entity]:
    first_key = (len(_name_tokens(first.name)), len(first.name), first.name.casefold())
    second_key = (
        len(_name_tokens(second.name)),
        len(second.name),
        second.name.casefold(),
    )
    return (first, second) if first_key > second_key else (second, first)


def generate_alias_candidates(session: Session) -> list[dict[str, Any]]:
    """Persist and return same-type/book, co-mentioned short/full-name pairs."""
    entities = session.exec(
        select(Entity)
        .where(Entity.source_book.is_not(None))
        .order_by(Entity.entity_type, Entity.source_book, Entity.name, Entity.id)
    ).all()
    mentions: dict[str, set[str]] = {}
    for chunk_id, entity_id in session.exec(
        select(ChunkEntityMention.chunk_id, ChunkEntityMention.entity_id)
    ).all():
        mentions.setdefault(str(entity_id), set()).add(str(chunk_id))

    found: list[EntityAliasReview] = []
    for index, first in enumerate(entities):
        for second in entities[index + 1 :]:
            if (second.entity_type, second.source_book) != (
                first.entity_type,
                first.source_book,
            ):
                if (second.entity_type, second.source_book) > (
                    first.entity_type,
                    first.source_book,
                ):
                    break
                continue
            if first.entity_type == "location" and first.site != second.site:
                continue
            if not _is_name_pair(first, second):
                continue
            evidence_ids = sorted(
                mentions.get(str(first.id), set()) & mentions.get(str(second.id), set())
            )
            if not evidence_ids:
                continue
            survivor, twin = _survivor_and_twin(first, second)
            review = session.get(EntityAliasReview, (survivor.id, twin.id))
            if review is None:
                review = EntityAliasReview(
                    survivor_id=survivor.id,
                    twin_id=twin.id,
                    evidence_chunk_ids=evidence_ids,
                )
                session.add(review)
            elif review.status != "merged":
                review.evidence_chunk_ids = evidence_ids
            found.append(review)
    session.commit()
    return alias_review_report(
        session, pairs=[(r.survivor_id, r.twin_id) for r in found]
    )


def alias_review_report(
    session: Session,
    *,
    status: str | None = None,
    pairs: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Render persisted candidates with names and co-mention snippets."""
    statement = select(EntityAliasReview).order_by(EntityAliasReview.created_at)
    if status is not None:
        statement = statement.where(EntityAliasReview.status == status)
    reviews = session.exec(statement).all()
    if pairs is not None:
        wanted = set(pairs)
        reviews = [r for r in reviews if (r.survivor_id, r.twin_id) in wanted]
    report: list[dict[str, Any]] = []
    for review in reviews:
        survivor = session.get(Entity, review.survivor_id)
        twin = session.get(Entity, review.twin_id)
        evidence = []
        for chunk_id in review.evidence_chunk_ids:
            chunk = session.get(KnowledgeChunk, chunk_id)
            evidence.append(
                {
                    "chunk_id": chunk_id,
                    "snippet": chunk.content[:400] if chunk is not None else None,
                }
            )
        report.append(
            {
                "survivor_id": review.survivor_id,
                "survivor_name": survivor.name if survivor else None,
                "twin_id": review.twin_id,
                "twin_name": twin.name if twin else None,
                "status": review.status,
                "evidence": evidence,
                "reviewed_by": review.reviewed_by,
                "review_note": review.review_note,
                "reviewed_at": review.reviewed_at,
                "merged_at": review.merged_at,
            }
        )
    return report


def record_alias_decision(
    session: Session,
    survivor_id: str,
    twin_id: str,
    decision: Literal["approved", "rejected"],
    reviewed_by: str,
    review_note: str | None = None,
) -> EntityAliasReview:
    """Record an explicit human decision without executing a merge."""
    if not reviewed_by.strip():
        raise ValueError("reviewed_by is required")
    review = session.get(EntityAliasReview, (survivor_id, twin_id))
    if review is None:
        raise LookupError("alias candidate not found")
    if review.status == "merged":
        raise ValueError("a merged alias decision is immutable")
    review.status = decision
    review.reviewed_by = reviewed_by.strip()
    review.review_note = review_note
    review.reviewed_at = datetime.now(timezone.utc)
    session.add(review)
    session.commit()
    session.refresh(review)
    return review


def _merge_json(twin: Any, survivor: Any) -> Any:
    """Recursively merge objects with survivor values taking precedence."""
    if not isinstance(twin, dict) or not isinstance(survivor, dict):
        return survivor if survivor not in (None, {}, []) else twin
    merged = dict(twin)
    for key, survivor_value in survivor.items():
        twin_value = twin.get(key)
        merged[key] = (
            _merge_json(twin_value, survivor_value)
            if isinstance(twin_value, dict) and isinstance(survivor_value, dict)
            else survivor_value
        )
    return merged


def _merge_detail(session: Session, survivor: Entity, twin: Entity) -> None:
    survivor.detail = _merge_json(twin.detail, survivor.detail)
    if survivor.temporality is None:
        survivor.temporality = twin.temporality
    detail_model = ENTITY_DETAIL_MODELS.get(survivor.entity_type)
    if detail_model is None:
        return
    survivor_detail = session.get(detail_model, survivor.id)
    twin_detail = session.get(detail_model, twin.id)
    if twin_detail is None:
        return
    if survivor_detail is None:
        values = {
            name: getattr(twin_detail, name)
            for name in detail_model.model_fields
            if name != "entity_id"
        }
        survivor_detail = detail_model(entity_id=survivor.id, **values)
        session.add(survivor_detail)
    else:
        for name in detail_model.model_fields:
            if name == "entity_id":
                continue
            setattr(
                survivor_detail,
                name,
                _merge_json(getattr(twin_detail, name), getattr(survivor_detail, name)),
            )
    session.delete(twin_detail)


def _repoint_mentions(session: Session, survivor_id: str, twin_id: str) -> None:
    twin_mentions = session.exec(
        select(ChunkEntityMention).where(ChunkEntityMention.entity_id == twin_id)
    ).all()
    for mention in twin_mentions:
        existing = session.get(ChunkEntityMention, (mention.chunk_id, survivor_id))
        if existing is not None:
            # Preserve the mention already attached to the survivor as the
            # first-writer provenance for this natural key.
            session.delete(mention)
        else:
            replacement = ChunkEntityMention(
                chunk_id=mention.chunk_id,
                entity_id=survivor_id,
                mention_text=mention.mention_text,
            )
            session.delete(mention)
            session.add(replacement)
    session.flush()


def _repoint_relationships(session: Session, survivor_id: str, twin_id: str) -> None:
    relationships = session.exec(
        select(Relationship)
        .where(
            or_(
                Relationship.from_entity_id == twin_id,
                Relationship.to_entity_id == twin_id,
            )
        )
        .order_by(Relationship.id)
    ).all()
    for relationship in relationships:
        new_from = (
            survivor_id
            if relationship.from_entity_id == twin_id
            else relationship.from_entity_id
        )
        new_to = (
            survivor_id
            if relationship.to_entity_id == twin_id
            else relationship.to_entity_id
        )
        if new_from == new_to:
            session.delete(relationship)
            continue
        existing = session.exec(
            select(Relationship).where(
                Relationship.from_entity_id == new_from,
                Relationship.to_entity_id == new_to,
                Relationship.rel_type == relationship.rel_type,
                Relationship.id != relationship.id,
            )
        ).first()
        if existing is not None:
            # The row already on the survivor keeps its properties and chunk_id,
            # retaining first-writer provenance across a unique-triple collision.
            session.delete(relationship)
        else:
            relationship.from_entity_id = new_from
            relationship.to_entity_id = new_to
    session.flush()


def _embedding_text(session: Session, entity: Entity) -> str:
    payload: dict[str, Any] = {"detail": entity.detail}
    detail_model = ENTITY_DETAIL_MODELS.get(entity.entity_type)
    if detail_model is not None:
        detail = session.get(detail_model, entity.id)
        if detail is not None:
            payload["typed_detail"] = {
                name: getattr(detail, name)
                for name in detail_model.model_fields
                if name != "entity_id" and getattr(detail, name) not in (None, {}, [])
            }
    return f"{entity.name}: {json.dumps(payload, sort_keys=True, default=str)}"


def _verification_snapshot(marker: EntityVerification) -> dict[str, Any]:
    """Serialize a marker before its entity key can be invalidated or deleted."""
    return {
        "entity_id": marker.entity_id,
        "verifier_version": marker.verifier_version,
        "model": marker.model,
        "status": marker.status,
        "evidence_chunk_ids": marker.evidence_chunk_ids,
        "corrections": marker.corrections,
        "verified_at": marker.verified_at.isoformat(),
    }


def _archive_and_invalidate_verification(
    session: Session,
    review: EntityAliasReview,
    survivor_id: str,
    twin_id: str,
) -> None:
    """Keep audit provenance while forcing the enriched survivor through verification."""
    markers = session.exec(
        select(EntityVerification)
        .where(EntityVerification.entity_id.in_([survivor_id, twin_id]))
        .order_by(EntityVerification.entity_id, EntityVerification.verifier_version)
    ).all()
    review.verification_history = [
        *(review.verification_history or []),
        *(_verification_snapshot(marker) for marker in markers),
    ]
    for marker in markers:
        session.delete(marker)
    for retry in session.exec(
        select(EntityVerificationRetry).where(
            EntityVerificationRetry.entity_id.in_([survivor_id, twin_id])
        )
    ).all():
        session.delete(retry)


async def _merge_pair(
    session: Session, embedder: Embedder, survivor_id: str, twin_id: str
) -> None:
    review = session.exec(
        select(EntityAliasReview)
        .where(
            EntityAliasReview.survivor_id == survivor_id,
            EntityAliasReview.twin_id == twin_id,
        )
        .with_for_update()
    ).first()
    if review is None or review.status != "approved" or not review.reviewed_by:
        raise AliasMergeError("pair has no explicit approval")
    survivor = session.exec(
        select(Entity).where(Entity.id == survivor_id).with_for_update()
    ).first()
    twin = session.exec(
        select(Entity).where(Entity.id == twin_id).with_for_update()
    ).first()
    if survivor is None or twin is None:
        raise AliasMergeError("approved pair references a missing entity")
    if (survivor.entity_type, survivor.source_book) != (
        twin.entity_type,
        twin.source_book,
    ) or not _is_name_pair(survivor, twin):
        raise AliasMergeError("approved pair no longer satisfies candidate rules")
    if survivor.entity_type == "location" and survivor.site != twin.site:
        raise AliasMergeError("approved locations no longer have compatible sites")
    if (
        session.exec(
            select(KnowledgeGrant.id).where(KnowledgeGrant.entity_id == twin_id)
        ).first()
        is not None
    ):
        # ADR 014 forbids this pass from changing knowledge grants. Do not rely
        # on ON DELETE CASCADE, which would silently revoke campaign knowledge.
        raise AliasMergeError("twin has knowledge grants and cannot be merged safely")

    _repoint_mentions(session, survivor_id, twin_id)
    _repoint_relationships(session, survivor_id, twin_id)
    _merge_detail(session, survivor, twin)
    _archive_and_invalidate_verification(session, review, survivor_id, twin_id)
    for embedding in session.exec(
        select(Embedding).where(
            Embedding.embeddable_kind == "entity",
            Embedding.embeddable_id == twin_id,
        )
    ).all():
        session.delete(embedding)

    vectors = await embedder.embed_batch([_embedding_text(session, survivor)])
    if len(vectors) != 1 or not vectors[0]:
        raise AliasMergeError("embedder returned no survivor vector")
    survivor_embedding = session.exec(
        select(Embedding).where(
            Embedding.embeddable_kind == "entity",
            Embedding.embeddable_id == survivor_id,
            Embedding.model == embedder.model,
        )
    ).first()
    if survivor_embedding is None:
        session.add(
            Embedding(
                embeddable_kind="entity",
                embeddable_id=survivor_id,
                model=embedder.model,
                dim=len(vectors[0]),
                vector=vectors[0],
            )
        )
    else:
        survivor_embedding.vector = vectors[0]
        survivor_embedding.dim = len(vectors[0])

    session.delete(twin)
    review.status = "merged"
    review.merged_at = datetime.now(timezone.utc)
    review.merge_attempts = 0
    review.merge_retry_after = None
    review.merge_error = None
    session.add(review)
    session.flush()


async def merge_approved_aliases(
    session: Session,
    embedder: Embedder,
    *,
    survivor_id: str | None = None,
    twin_id: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Merge approved pairs, committing or rolling back each pair atomically."""
    statement = select(EntityAliasReview.survivor_id, EntityAliasReview.twin_id).where(
        EntityAliasReview.status == "approved"
    )
    if survivor_id is not None or twin_id is not None:
        if not survivor_id or not twin_id:
            raise ValueError("survivor_id and twin_id must be supplied together")
        statement = statement.where(
            EntityAliasReview.survivor_id == survivor_id,
            EntityAliasReview.twin_id == twin_id,
        )
    else:
        now = datetime.now(timezone.utc)
        statement = statement.where(
            or_(
                EntityAliasReview.merge_retry_after.is_(None),
                EntityAliasReview.merge_retry_after <= now,
            )
        )
    pairs = list(
        session.exec(
            statement.order_by(
                EntityAliasReview.reviewed_at,
                EntityAliasReview.survivor_id,
                EntityAliasReview.twin_id,
            )
        ).all()
    )[:limit]
    # End the report query transaction before starting one independent unit per pair.
    session.rollback()
    summary: dict[str, Any] = {
        "approved_seen": len(pairs),
        "merged": 0,
        "failed": 0,
        "errors": [],
    }
    for survivor_key, twin_key in pairs:
        try:
            await _merge_pair(session, embedder, survivor_key, twin_key)
            session.commit()
            summary["merged"] += 1
        # Every pair failure must roll back and remain retryable.
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            summary["failed"] += 1
            summary["errors"].append(
                {"survivor_id": survivor_key, "twin_id": twin_key, "error": str(exc)}
            )
            review = session.get(EntityAliasReview, (survivor_key, twin_key))
            if review is not None and review.status == "approved":
                review.merge_attempts += 1
                review.merge_retry_after = datetime.now(timezone.utc) + timedelta(
                    seconds=min(
                        MERGE_RETRY_MAX_SECONDS,
                        MERGE_RETRY_BASE_SECONDS * 2 ** (review.merge_attempts - 1),
                    )
                )
                review.merge_error = str(exc)[:2000]
                session.add(review)
                session.commit()
    return summary
