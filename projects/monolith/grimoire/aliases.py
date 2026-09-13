"""Review-first Grimoire short/full-name alias reconciliation.

Candidate generation is deliberately conservative and DB-only. Execution is
approval-gated, revalidates the reviewed state under row locks, computes any
replacement embedding before mutation, and commits one candidate atomically.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from grimoire.models import (
    ENTITY_DETAIL_MODELS,
    AliasCandidate,
    ChunkEntityMention,
    Embedding,
    Entity,
    KnowledgeChunk,
    KnowledgeGrant,
    Relationship,
)

SIGNAL_VERSION = "short-full-comention-v2"
MAX_EVIDENCE = 3
SNIPPET_CHARS = 320
MAX_CONFLICT_RETRIES = 3
_SCAN_ADVISORY_LOCK = 6_503_913
_MAP_KEY_PREFIX_RE = re.compile(r"^[A-Za-z]{0,2}\d+[A-Za-z]?[.:]\s+")
_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_QUOTE_NORMALIZE = {"‘": "'", "’": "'", "“": '"', "”": '"'}


class AliasError(RuntimeError):
    """Base class for review or execution conflicts."""


class AliasNotFound(AliasError):
    pass


class ApprovalRequired(AliasError):
    pass


class StaleApproval(AliasError):
    pass


def _retryable_conflict(exc: DBAPIError) -> bool:
    code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return code in {"40001", "40P01"}


def _dialect_name(session: Session) -> str:
    return session.get_bind().dialect.name


def _lock_candidate(session: Session, candidate_id: str) -> AliasCandidate | None:
    return session.exec(
        select(AliasCandidate)
        .where(AliasCandidate.id == candidate_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


def _lock_review_state(
    session: Session, entity_ids: list[str]
) -> tuple[dict[str, Entity], list[ChunkEntityMention]]:
    """Lock every row whose contents form the reviewed alias snapshot."""
    ordered_ids = sorted(entity_ids)
    entities = session.exec(
        select(Entity)
        .where(Entity.id.in_(ordered_ids))
        .order_by(Entity.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    by_id = {entity.id: entity for entity in entities}

    for entity in entities:
        detail_model = ENTITY_DETAIL_MODELS.get(entity.entity_type)
        if detail_model is not None:
            session.exec(
                select(detail_model)
                .where(detail_model.entity_id == entity.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()

    mentions = session.exec(
        select(ChunkEntityMention)
        .where(ChunkEntityMention.entity_id.in_(ordered_ids))
        .order_by(ChunkEntityMention.chunk_id, ChunkEntityMention.entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    chunk_ids = sorted({mention.chunk_id for mention in mentions})
    if chunk_ids:
        session.exec(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.id.in_(chunk_ids))
            .order_by(KnowledgeChunk.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    return by_id, mentions


def _lock_merge_children(session: Session, entity_ids: list[str]) -> None:
    """Lock mutable merge inputs in a deterministic table and row order."""
    ordered_ids = sorted(entity_ids)
    session.exec(
        select(Relationship)
        .where(
            or_(
                Relationship.from_entity_id.in_(ordered_ids),
                Relationship.to_entity_id.in_(ordered_ids),
            )
        )
        .order_by(Relationship.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    session.exec(
        select(KnowledgeGrant)
        .where(KnowledgeGrant.entity_id.in_(ordered_ids))
        .order_by(KnowledgeGrant.player_character_id, KnowledgeGrant.entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    session.exec(
        select(Embedding)
        .where(
            Embedding.embeddable_kind == "entity",
            Embedding.embeddable_id.in_(ordered_ids),
        )
        .order_by(Embedding.embeddable_id, Embedding.model, Embedding.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()


def _tokens(name: str) -> tuple[str, ...]:
    for curly, straight in _QUOTE_NORMALIZE.items():
        name = name.replace(curly, straight)
    canonical = _MAP_KEY_PREFIX_RE.sub("", name.strip())
    canonical = _LEADING_ARTICLE_RE.sub("", canonical).strip().casefold()
    return tuple(re.findall(r"[^\W_]+(?:['’][^\W_]+)*", canonical))


def _name_pair(left: Entity, right: Entity) -> tuple[Entity, Entity] | None:
    """Return (short, full) when the ADR's strict name signal holds."""
    left_tokens = _tokens(left.name)
    right_tokens = _tokens(right.name)
    if not left_tokens or not right_tokens or left_tokens == right_tokens:
        return None

    left_set = set(left_tokens)
    right_set = set(right_tokens)
    left_prefix = (
        len(left_tokens) < len(right_tokens)
        and right_tokens[: len(left_tokens)] == left_tokens
    )
    right_prefix = (
        len(right_tokens) < len(left_tokens)
        and left_tokens[: len(right_tokens)] == right_tokens
    )
    if left_prefix or left_set < right_set:
        return left, right
    if right_prefix or right_set < left_set:
        return right, left
    return None


def _sites_compatible(short: Entity, full: Entity) -> bool:
    if short.entity_type != "location":
        return True
    return not short.site or not full.site or short.site == full.site


def _snippet(content: str, names: tuple[str, str]) -> str:
    folded = content.casefold()
    offsets = [folded.find(name.casefold()) for name in names]
    found = [offset for offset in offsets if offset >= 0]
    center = min(found) if found else 0
    start = max(0, center - SNIPPET_CHARS // 4)
    end = min(len(content), start + SNIPPET_CHARS)
    value = " ".join(content[start:end].split())
    if start:
        value = "..." + value
    if end < len(content):
        value += "..."
    return value


def _evidence_row(
    chunk: KnowledgeChunk,
    short: Entity,
    full: Entity,
    short_mention: str | None,
    full_mention: str | None,
) -> dict[str, Any]:
    return {
        "chunk_id": chunk.id,
        "chunk_ref": chunk.chunk_ref,
        "short_mention": short_mention,
        "full_mention": full_mention,
        "snippet": _snippet(chunk.content, (short.name, full.name)),
    }


def _all_mentions(session: Session, entity_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            ChunkEntityMention.chunk_id,
            ChunkEntityMention.mention_text,
        )
        .where(ChunkEntityMention.entity_id == entity_id)
        .order_by(ChunkEntityMention.chunk_id)
    ).all()
    return [
        {"chunk_id": chunk_id, "mention_text": mention_text}
        for chunk_id, mention_text in rows
    ]


def _co_mentions(
    session: Session, short: Entity, full: Entity
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    short_mention = aliased(ChunkEntityMention)
    full_mention = aliased(ChunkEntityMention)
    rows = session.execute(
        select(
            KnowledgeChunk,
            short_mention.mention_text,
            full_mention.mention_text,
        )
        .join(short_mention, short_mention.chunk_id == KnowledgeChunk.id)
        .join(full_mention, full_mention.chunk_id == KnowledgeChunk.id)
        .where(
            short_mention.entity_id == short.id,
            full_mention.entity_id == full.id,
        )
        .order_by(KnowledgeChunk.id)
    ).all()
    fingerprint_rows = [
        {
            "chunk_id": chunk.id,
            "content_hash": hashlib.sha256(chunk.content.encode()).hexdigest(),
            "short_mention": short_text,
            "full_mention": full_text,
        }
        for chunk, short_text, full_text in rows
    ]
    report_rows = [
        _evidence_row(chunk, short, full, short_text, full_text)
        for chunk, short_text, full_text in rows[:MAX_EVIDENCE]
    ]
    return report_rows, fingerprint_rows


def _entity_snapshot(session: Session, entity: Entity) -> dict[str, Any]:
    detail_row = None
    detail_model = ENTITY_DETAIL_MODELS.get(entity.entity_type)
    if detail_model is not None:
        stored = session.get(detail_model, entity.id)
        if stored is not None:
            detail_row = stored.model_dump(exclude={"entity_id"})
    return {
        "id": entity.id,
        "entity_type": entity.entity_type,
        "name": entity.name,
        "source_book": entity.source_book,
        "site": entity.site,
        "temporality": entity.temporality,
        "source_type": entity.source_type,
        "is_global": entity.is_global,
        "detail": entity.detail,
        "typed_detail": detail_row,
        "mentions": _all_mentions(session, entity.id),
    }


def _state_hash(
    session: Session,
    short: Entity,
    full: Entity,
    co_mentions: list[dict[str, Any]],
) -> str:
    payload = {
        "signal_version": SIGNAL_VERSION,
        "short": _entity_snapshot(session, short),
        "full": _entity_snapshot(session, full),
        "co_mentions": co_mentions,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_pair(
    session: Session, short: Entity | None, full: Entity | None
) -> tuple[list[dict[str, Any]], int, str]:
    if short is None or full is None:
        raise StaleApproval("one or both reviewed entities no longer exist")
    if (
        short.entity_type != full.entity_type
        or not short.source_book
        or short.source_book != full.source_book
    ):
        raise StaleApproval("reviewed entities are no longer the same type and book")
    pair = _name_pair(short, full)
    if pair is None or pair[0].id != short.id or pair[1].id != full.id:
        raise StaleApproval(
            "reviewed entities no longer have the short/full-name signal"
        )
    if not _sites_compatible(short, full):
        raise StaleApproval("reviewed locations now have incompatible sites")
    report_evidence, fingerprint_evidence = _co_mentions(session, short, full)
    if not fingerprint_evidence:
        raise StaleApproval("reviewed entities no longer have co-mention evidence")
    return (
        report_evidence,
        len(fingerprint_evidence),
        _state_hash(session, short, full, fingerprint_evidence),
    )


def candidate_view(candidate: AliasCandidate) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "status": candidate.status,
        "entity_type": candidate.entity_type,
        "source_book": candidate.source_book,
        "short_entity_id": candidate.short_entity_id,
        "short_name": candidate.short_name,
        "short_site": candidate.short_site,
        "short_temporality": candidate.short_temporality,
        "full_entity_id": candidate.full_entity_id,
        "full_name": candidate.full_name,
        "full_site": candidate.full_site,
        "full_temporality": candidate.full_temporality,
        "survivor_entity_id": candidate.survivor_entity_id,
        "evidence_count": candidate.evidence_count,
        "evidence": candidate.evidence,
        "state_hash": candidate.state_hash,
        "approved_state_hash": candidate.approved_state_hash,
        "approved_by": candidate.approved_by,
        "approved_at": candidate.approved_at,
        "rejected_state_hash": candidate.rejected_state_hash,
        "rejected_by": candidate.rejected_by,
        "rejected_at": candidate.rejected_at,
        "reopened_by": candidate.reopened_by,
        "reopened_at": candidate.reopened_at,
        "merged_at": candidate.merged_at,
        "updated_at": candidate.updated_at,
    }


def list_candidates(
    session: Session, status: str | None = None
) -> list[dict[str, Any]]:
    statement = select(AliasCandidate)
    if status is not None:
        statement = statement.where(AliasCandidate.status == status)
    rows = session.exec(
        statement.order_by(AliasCandidate.updated_at.desc(), AliasCandidate.id)
    ).all()
    return [candidate_view(row) for row in rows]


def _generate_candidates_once(session: Session) -> dict[str, Any]:
    if _dialect_name(session) == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _SCAN_ADVISORY_LOCK},
        )
    locked_candidates = session.exec(
        select(AliasCandidate)
        .order_by(AliasCandidate.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()

    left_entity = aliased(Entity)
    right_entity = aliased(Entity)
    left_mention = aliased(ChunkEntityMention)
    right_mention = aliased(ChunkEntityMention)
    rows = session.execute(
        select(
            left_entity,
            right_entity,
            KnowledgeChunk,
            left_mention.mention_text,
            right_mention.mention_text,
        )
        .join(left_mention, left_mention.chunk_id == KnowledgeChunk.id)
        .join(right_mention, right_mention.chunk_id == KnowledgeChunk.id)
        .join(left_entity, left_entity.id == left_mention.entity_id)
        .join(right_entity, right_entity.id == right_mention.entity_id)
        .where(
            left_entity.id < right_entity.id,
            left_entity.entity_type == right_entity.entity_type,
            left_entity.source_book.is_not(None),
            left_entity.source_book == right_entity.source_book,
        )
        .order_by(left_entity.id, right_entity.id, KnowledgeChunk.id)
    ).all()

    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for left, right, chunk, left_text, right_text in rows:
        pair = _name_pair(left, right)
        if pair is None or not _sites_compatible(*pair):
            continue
        short, full = pair
        key = (short.id, full.id)
        entry = pairs.setdefault(
            key,
            {"short": short, "full": full, "evidence": [], "fingerprint": []},
        )
        if short.id == left.id:
            short_text, full_text = left_text, right_text
        else:
            short_text, full_text = right_text, left_text
        entry["fingerprint"].append(
            {
                "chunk_id": chunk.id,
                "content_hash": hashlib.sha256(chunk.content.encode()).hexdigest(),
                "short_mention": short_text,
                "full_mention": full_text,
            }
        )
        if len(entry["evidence"]) < MAX_EVIDENCE:
            entry["evidence"].append(
                _evidence_row(chunk, short, full, short_text, full_text)
            )

    existing = {
        (row.short_entity_id, row.full_entity_id): row for row in locked_candidates
    }
    now = datetime.now(timezone.utc)
    created = updated = staled = 0
    current_keys = set(pairs)

    for key, found in pairs.items():
        short = found["short"]
        full = found["full"]
        fingerprint = found["fingerprint"]
        fingerprint.sort(key=lambda item: item["chunk_id"])
        current_hash = _state_hash(session, short, full, fingerprint)
        candidate = existing.get(key)
        if candidate is None:
            candidate = AliasCandidate(
                short_entity_id=short.id,
                full_entity_id=full.id,
                entity_type=short.entity_type,
                source_book=short.source_book,
                short_name=short.name,
                full_name=full.name,
                short_site=short.site,
                full_site=full.site,
                short_temporality=short.temporality,
                full_temporality=full.temporality,
                signal_version=SIGNAL_VERSION,
                evidence=found["evidence"],
                evidence_count=len(fingerprint),
                state_hash=current_hash,
            )
            session.add(candidate)
            existing[key] = candidate
            created += 1
            continue

        changed = candidate.state_hash != current_hash
        candidate.entity_type = short.entity_type
        candidate.source_book = short.source_book
        candidate.short_name = short.name
        candidate.full_name = full.name
        candidate.short_site = short.site
        candidate.full_site = full.site
        candidate.short_temporality = short.temporality
        candidate.full_temporality = full.temporality
        candidate.signal_version = SIGNAL_VERSION
        candidate.evidence = found["evidence"]
        candidate.evidence_count = len(fingerprint)
        candidate.state_hash = current_hash
        if changed and candidate.status == "approved":
            candidate.status = "stale"
            staled += 1
        if changed:
            candidate.updated_at = now
            updated += 1

    for key, candidate in existing.items():
        if key not in current_keys and candidate.status in {"pending", "approved"}:
            candidate.status = "stale"
            candidate.updated_at = now
            staled += 1

    session.commit()
    return {
        "created": created,
        "updated": updated,
        "staled": staled,
        "candidates": list_candidates(session),
    }


def generate_candidates(session: Session) -> dict[str, Any]:
    """Generate or refresh durable candidates and return the review report."""
    for attempt in range(MAX_CONFLICT_RETRIES):
        try:
            return _generate_candidates_once(session)
        except DBAPIError as exc:
            session.rollback()
            if not _retryable_conflict(exc) or attempt + 1 == MAX_CONFLICT_RETRIES:
                raise
    raise AssertionError("unreachable")


def _mark_stale(
    session: Session, candidate: AliasCandidate, reason: StaleApproval
) -> None:
    candidate.status = "stale"
    candidate.updated_at = datetime.now(timezone.utc)
    session.commit()
    raise reason


def _current_locked_state(
    session: Session, candidate: AliasCandidate
) -> tuple[list[dict[str, Any]], int, str, Entity, Entity]:
    entities, _ = _lock_review_state(
        session, [candidate.short_entity_id, candidate.full_entity_id]
    )
    short = entities.get(candidate.short_entity_id)
    full = entities.get(candidate.full_entity_id)
    evidence, evidence_count, current_hash = _validate_pair(session, short, full)
    return evidence, evidence_count, current_hash, short, full


def _require_expected_state(
    session: Session, candidate: AliasCandidate, expected_state_hash: str
) -> tuple[list[dict[str, Any]], int, str, Entity, Entity]:
    if candidate.state_hash != expected_state_hash:
        session.rollback()
        raise StaleApproval("candidate report changed before the decision")
    try:
        state = _current_locked_state(session, candidate)
    except StaleApproval as exc:
        _mark_stale(session, candidate, exc)
    if state[2] != expected_state_hash:
        candidate.state_hash = state[2]
        candidate.evidence = state[0]
        candidate.evidence_count = state[1]
        _mark_stale(session, candidate, StaleApproval("candidate changed since review"))
    return state


def _approve_candidate_once(
    session: Session,
    candidate_id: str,
    reviewer: str,
    survivor_id: str,
    expected_state_hash: str,
) -> dict[str, Any]:
    candidate = _lock_candidate(session, candidate_id)
    if candidate is None:
        raise AliasNotFound("alias candidate not found")
    if candidate.status == "merged":
        session.rollback()
        return candidate_view(candidate)
    if survivor_id != candidate.full_entity_id:
        session.rollback()
        raise AliasError("the longer full-name entity must be the survivor")
    _, _, current_hash, _, _ = _require_expected_state(
        session, candidate, expected_state_hash
    )
    if candidate.status in {"rejected", "stale"}:
        session.rollback()
        raise AliasError("rejected or stale candidates must be explicitly reopened")

    if (
        candidate.status == "approved"
        and candidate.approved_state_hash == current_hash
        and candidate.survivor_entity_id == survivor_id
    ):
        session.rollback()
        return candidate_view(candidate)

    now = datetime.now(timezone.utc)
    candidate.status = "approved"
    candidate.survivor_entity_id = survivor_id
    candidate.approved_state_hash = current_hash
    candidate.approved_by = reviewer
    candidate.approved_at = now
    candidate.updated_at = now
    session.commit()
    return candidate_view(candidate)


def approve_candidate(
    session: Session,
    candidate_id: str,
    reviewer: str,
    survivor_id: str,
    expected_state_hash: str,
) -> dict[str, Any]:
    """Approve exactly the candidate state named in the human's report."""
    for attempt in range(MAX_CONFLICT_RETRIES):
        try:
            return _approve_candidate_once(
                session,
                candidate_id,
                reviewer,
                survivor_id,
                expected_state_hash,
            )
        except DBAPIError as exc:
            session.rollback()
            if not _retryable_conflict(exc) or attempt + 1 == MAX_CONFLICT_RETRIES:
                raise
    raise AssertionError("unreachable")


def _reject_candidate_once(
    session: Session,
    candidate_id: str,
    reviewer: str,
    expected_state_hash: str,
) -> dict[str, Any]:
    candidate = _lock_candidate(session, candidate_id)
    if candidate is None:
        raise AliasNotFound("alias candidate not found")
    if candidate.status == "merged":
        session.rollback()
        raise AliasError("merged candidates cannot be rejected")
    _require_expected_state(session, candidate, expected_state_hash)
    if (
        candidate.status == "rejected"
        and candidate.rejected_state_hash == expected_state_hash
        and candidate.rejected_by == reviewer
    ):
        session.rollback()
        return candidate_view(candidate)
    now = datetime.now(timezone.utc)
    candidate.status = "rejected"
    candidate.rejected_state_hash = expected_state_hash
    candidate.rejected_by = reviewer
    candidate.rejected_at = now
    candidate.updated_at = now
    session.commit()
    return candidate_view(candidate)


def reject_candidate(
    session: Session,
    candidate_id: str,
    reviewer: str,
    expected_state_hash: str,
) -> dict[str, Any]:
    """Persist a human rejection without allowing scans to reopen it."""
    for attempt in range(MAX_CONFLICT_RETRIES):
        try:
            return _reject_candidate_once(
                session, candidate_id, reviewer, expected_state_hash
            )
        except DBAPIError as exc:
            session.rollback()
            if not _retryable_conflict(exc) or attempt + 1 == MAX_CONFLICT_RETRIES:
                raise
    raise AssertionError("unreachable")


def _reopen_candidate_once(
    session: Session,
    candidate_id: str,
    reviewer: str,
    expected_state_hash: str,
) -> dict[str, Any]:
    candidate = _lock_candidate(session, candidate_id)
    if candidate is None:
        raise AliasNotFound("alias candidate not found")
    if candidate.status not in {"rejected", "stale"}:
        session.rollback()
        raise AliasError("only rejected or stale candidates can be reopened")
    _require_expected_state(session, candidate, expected_state_hash)
    now = datetime.now(timezone.utc)
    candidate.status = "pending"
    candidate.survivor_entity_id = None
    candidate.approved_state_hash = None
    candidate.approved_by = None
    candidate.approved_at = None
    candidate.reopened_by = reviewer
    candidate.reopened_at = now
    candidate.updated_at = now
    session.commit()
    return candidate_view(candidate)


def reopen_candidate(
    session: Session,
    candidate_id: str,
    reviewer: str,
    expected_state_hash: str,
) -> dict[str, Any]:
    """Deliberately put a rejected or stale current state back in review."""
    for attempt in range(MAX_CONFLICT_RETRIES):
        try:
            return _reopen_candidate_once(
                session, candidate_id, reviewer, expected_state_hash
            )
        except DBAPIError as exc:
            session.rollback()
            if not _retryable_conflict(exc) or attempt + 1 == MAX_CONFLICT_RETRIES:
                raise
    raise AssertionError("unreachable")


def _mention_texts(session: Session, entity_id: str) -> list[str]:
    texts = session.exec(
        select(ChunkEntityMention.mention_text)
        .where(
            ChunkEntityMention.entity_id == entity_id,
            ChunkEntityMention.mention_text.is_not(None),
        )
        .order_by(ChunkEntityMention.chunk_id)
    ).all()
    return sorted({text.strip() for text in texts if text and text.strip()})


def _embedding_text(name: str, mention_texts: list[str]) -> str:
    summary = " | ".join(mention_texts)
    return f"{name}: {summary}"


@dataclass(frozen=True)
class _MergePlan:
    candidate_id: str
    state_hash: str
    embedding_needed: bool
    embedding_text: str
    replay: bool = False


def _has_current_embedding(
    session: Session, entity_id: str, embedding_model: str
) -> bool:
    return (
        session.exec(
            select(Embedding.id).where(
                Embedding.embeddable_kind == "entity",
                Embedding.embeddable_id == entity_id,
                Embedding.model == embedding_model,
            )
        ).first()
        is not None
    )


def _prepare_merge(
    session: Session, candidate_id: str, embedding_model: str
) -> _MergePlan:
    candidate = session.get(AliasCandidate, candidate_id)
    if candidate is None:
        raise AliasNotFound("alias candidate not found")
    candidate = _lock_candidate(session, candidate_id)
    if candidate is None:
        raise AliasNotFound("alias candidate not found")
    if candidate.status == "merged":
        return _MergePlan(candidate_id, candidate.state_hash, False, "", replay=True)
    if candidate.status != "approved" or not candidate.approved_state_hash:
        raise ApprovalRequired("alias candidate has no current explicit approval")

    short = session.get(Entity, candidate.short_entity_id)
    full = session.get(Entity, candidate.full_entity_id)
    try:
        _, _, current_hash = _validate_pair(session, short, full)
    except StaleApproval as exc:
        _mark_stale(session, candidate, exc)
    if current_hash != candidate.approved_state_hash:
        candidate.state_hash = current_hash
        _mark_stale(session, candidate, StaleApproval("approval is stale"))

    before_texts = _mention_texts(session, full.id)
    after_texts = sorted(set(before_texts) | set(_mention_texts(session, short.id)))
    before = _embedding_text(full.name, before_texts)
    after = _embedding_text(full.name, after_texts)
    embedding_needed = before != after or not _has_current_embedding(
        session, full.id, embedding_model
    )
    return _MergePlan(candidate_id, current_hash, embedding_needed, after)


def _merge_prefer_primary(primary: Any, secondary: Any) -> Any:
    if primary is None:
        return deepcopy(secondary)
    if isinstance(primary, dict) and isinstance(secondary, dict):
        merged = deepcopy(secondary)
        for key, value in primary.items():
            merged[key] = _merge_prefer_primary(value, merged.get(key))
        return merged
    return deepcopy(primary)


def _merge_text(primary: str | None, secondary: str | None) -> str | None:
    values = []
    for value in (primary, secondary):
        if value and value not in values:
            values.append(value)
    return "\n".join(values) if values else None


def _merge_properties(
    primary: dict | None,
    others: list[dict | None],
    chunk_ids: list[str],
) -> dict:
    all_properties = [primary or {}, *(properties or {} for properties in others)]
    merged = {
        key: deepcopy(value)
        for key, value in (primary or {}).items()
        if key not in {"_alias_merge_conflicts", "_alias_merge_chunk_ids"}
    }
    conflicts: dict[str, list[Any]] = {}
    for properties in all_properties:
        history = properties.get("_alias_merge_conflicts")
        if not isinstance(history, dict):
            continue
        for path, history_values in history.items():
            if not isinstance(history_values, list):
                continue
            values = conflicts.setdefault(path, [])
            for value in history_values:
                if value not in values:
                    values.append(deepcopy(value))

    def add(target: dict, incoming: dict, prefix: str = "") -> None:
        for key in sorted(incoming):
            if key in {"_alias_merge_conflicts", "_alias_merge_chunk_ids"}:
                continue
            value = incoming[key]
            path = f"{prefix}.{key}" if prefix else key
            if key not in target:
                target[key] = deepcopy(value)
            elif isinstance(target[key], dict) and isinstance(value, dict):
                add(target[key], value, path)
            elif target[key] != value:
                values = conflicts.setdefault(path, [])
                if target[key] not in values:
                    values.append(deepcopy(target[key]))
                if value not in values:
                    values.append(deepcopy(value))

    for properties in others:
        add(merged, properties or {})
    if conflicts:
        merged["_alias_merge_conflicts"] = conflicts
    prior_chunks = set()
    for properties in all_properties:
        history_chunks = properties.get("_alias_merge_chunk_ids")
        if isinstance(history_chunks, list):
            prior_chunks.update(chunk_id for chunk_id in history_chunks if chunk_id)
    unique_chunks = sorted(
        prior_chunks | {chunk_id for chunk_id in chunk_ids if chunk_id}
    )
    if len(unique_chunks) > 1:
        merged["_alias_merge_chunk_ids"] = unique_chunks
    return merged


def _merge_typed_detail(session: Session, short: Entity, full: Entity) -> None:
    detail_model = ENTITY_DETAIL_MODELS.get(full.entity_type)
    if detail_model is None:
        return
    short_detail = session.get(detail_model, short.id)
    if short_detail is None:
        return
    full_detail = session.get(detail_model, full.id)
    values = short_detail.model_dump(exclude={"entity_id"})
    if full_detail is None:
        session.add(detail_model(entity_id=full.id, **values))
    else:
        for field_name, short_value in values.items():
            full_value = getattr(full_detail, field_name)
            setattr(
                full_detail,
                field_name,
                _merge_prefer_primary(full_value, short_value),
            )
    session.delete(short_detail)


def _rewrite_mentions(session: Session, short: Entity, full: Entity) -> None:
    mentions = session.exec(
        select(ChunkEntityMention).where(
            ChunkEntityMention.entity_id.in_([short.id, full.id])
        )
    ).all()
    by_chunk: dict[str, list[ChunkEntityMention]] = {}
    for mention in mentions:
        by_chunk.setdefault(mention.chunk_id, []).append(mention)
    updates: list[ChunkEntityMention] = []
    for rows in by_chunk.values():
        full_row = next((row for row in rows if row.entity_id == full.id), None)
        short_row = next((row for row in rows if row.entity_id == short.id), None)
        if full_row is not None and short_row is not None:
            full_row.mention_text = _merge_text(
                full_row.mention_text, short_row.mention_text
            )
            session.delete(short_row)
        elif short_row is not None:
            updates.append(short_row)
    session.flush()
    for row in updates:
        row.entity_id = full.id


def _rewrite_relationships(session: Session, short: Entity, full: Entity) -> None:
    relationships = session.exec(
        select(Relationship).where(
            or_(
                Relationship.from_entity_id.in_([short.id, full.id]),
                Relationship.to_entity_id.in_([short.id, full.id]),
            )
        )
    ).all()
    groups: dict[tuple[str, str, str], list[Relationship]] = {}
    self_loops: list[Relationship] = []
    for relationship in relationships:
        from_id = (
            full.id
            if relationship.from_entity_id == short.id
            else relationship.from_entity_id
        )
        to_id = (
            full.id
            if relationship.to_entity_id == short.id
            else relationship.to_entity_id
        )
        if from_id == to_id:
            self_loops.append(relationship)
            continue
        groups.setdefault((from_id, to_id, relationship.rel_type), []).append(
            relationship
        )
    for relationship in self_loops:
        session.delete(relationship)

    updates: list[tuple[Relationship, tuple[str, str, str]]] = []
    for key, rows in groups.items():
        rows.sort(
            key=lambda row: (
                row.from_entity_id == short.id or row.to_entity_id == short.id,
                row.id,
            )
        )
        winner, *duplicates = rows
        winner.properties = _merge_properties(
            winner.properties,
            [row.properties for row in duplicates],
            [row.chunk_id for row in rows if row.chunk_id],
        )
        if winner.chunk_id is None:
            winner.chunk_id = next(
                (row.chunk_id for row in rows if row.chunk_id is not None), None
            )
        for duplicate in duplicates:
            session.delete(duplicate)
        updates.append((winner, key))
    session.flush()
    for winner, (from_id, to_id, _) in updates:
        winner.from_entity_id = from_id
        winner.to_entity_id = to_id


_GRANT_SCOPE_RANK = {"name_only": 0, "partial": 1, "full": 2}


def _rewrite_grants(session: Session, short: Entity, full: Entity) -> None:
    grants = session.exec(
        select(KnowledgeGrant).where(KnowledgeGrant.entity_id.in_([short.id, full.id]))
    ).all()
    by_player: dict[str, list[KnowledgeGrant]] = {}
    for grant in grants:
        by_player.setdefault(grant.player_character_id, []).append(grant)
    updates: list[KnowledgeGrant] = []
    for rows in by_player.values():
        full_grant = next((row for row in rows if row.entity_id == full.id), None)
        short_grant = next((row for row in rows if row.entity_id == short.id), None)
        if full_grant is not None and short_grant is not None:
            if (
                _GRANT_SCOPE_RANK[short_grant.grant_scope]
                > _GRANT_SCOPE_RANK[full_grant.grant_scope]
            ):
                full_grant.grant_scope = short_grant.grant_scope
            full_grant.revealed_details = _merge_prefer_primary(
                full_grant.revealed_details, short_grant.revealed_details
            )
            session.delete(short_grant)
        elif short_grant is not None:
            updates.append(short_grant)
    session.flush()
    for row in updates:
        row.entity_id = full.id


def _apply_merge(
    session: Session,
    plan: _MergePlan,
    embedding_model: str,
    embedding_vector: list[float] | None,
) -> dict[str, Any]:
    candidate = _lock_candidate(session, plan.candidate_id)
    if candidate is None:
        raise AliasNotFound("alias candidate not found")
    if candidate.status == "merged":
        session.rollback()
        return {"status": "merged", "candidate_id": candidate.id, "replay": True}
    if candidate.status != "approved":
        session.rollback()
        raise ApprovalRequired("alias candidate has no current explicit approval")

    entity_ids = [candidate.short_entity_id, candidate.full_entity_id]
    by_id, _ = _lock_review_state(session, entity_ids)
    _lock_merge_children(session, entity_ids)
    short = by_id.get(candidate.short_entity_id)
    full = by_id.get(candidate.full_entity_id)
    try:
        _, _, current_hash = _validate_pair(session, short, full)
    except StaleApproval as exc:
        _mark_stale(session, candidate, exc)
    if (
        current_hash != plan.state_hash
        or current_hash != candidate.approved_state_hash
        or candidate.survivor_entity_id != full.id
    ):
        candidate.state_hash = current_hash
        _mark_stale(
            session, candidate, StaleApproval("approval changed before execution")
        )

    current_full_texts = _mention_texts(session, full.id)
    merged_texts = sorted(
        set(current_full_texts) | set(_mention_texts(session, short.id))
    )
    embedding_input_changed = _embedding_text(
        full.name, current_full_texts
    ) != _embedding_text(full.name, merged_texts)
    current_embedding_needed = embedding_input_changed or not _has_current_embedding(
        session, full.id, embedding_model
    )
    if current_embedding_needed != plan.embedding_needed:
        _mark_stale(session, candidate, StaleApproval("embedding input changed"))
    if current_embedding_needed and embedding_vector is None:
        session.rollback()
        raise AliasError("replacement embedding was not prepared")

    try:
        full.detail = _merge_prefer_primary(full.detail, short.detail)
        full.is_global = full.is_global or short.is_global
        if full.entity_type == "location" and full.site is None:
            full.site = short.site
        if full.entity_type in {"event", "quest"} and full.temporality is None:
            full.temporality = short.temporality
        _merge_typed_detail(session, short, full)
        _rewrite_mentions(session, short, full)
        _rewrite_relationships(session, short, full)
        _rewrite_grants(session, short, full)

        twin_embeddings = session.exec(
            select(Embedding).where(
                Embedding.embeddable_kind == "entity",
                Embedding.embeddable_id == short.id,
            )
        ).all()
        for embedding in twin_embeddings:
            session.delete(embedding)
        if current_embedding_needed:
            survivor_embeddings = session.exec(
                select(Embedding).where(
                    Embedding.embeddable_kind == "entity",
                    Embedding.embeddable_id == full.id,
                )
            ).all()
            current_embedding = next(
                (
                    embedding
                    for embedding in survivor_embeddings
                    if embedding.model == embedding_model
                ),
                None,
            )
            for embedding in survivor_embeddings:
                if embedding is not current_embedding:
                    session.delete(embedding)
            if current_embedding is None:
                session.flush()
                current_embedding = Embedding(
                    embeddable_kind="entity",
                    embeddable_id=full.id,
                    model=embedding_model,
                    dim=len(embedding_vector),
                    vector=embedding_vector,
                )
                session.add(current_embedding)
            else:
                current_embedding.dim = len(embedding_vector)
                current_embedding.vector = embedding_vector

        session.flush()
        session.delete(short)
        now = datetime.now(timezone.utc)
        candidate.status = "merged"
        candidate.merged_at = now
        candidate.updated_at = now
        session.commit()
    except Exception:
        session.rollback()
        raise
    return {
        "status": "merged",
        "candidate_id": candidate.id,
        "survivor_entity_id": full.id,
        "removed_entity_id": short.id,
        "embedding_refreshed": current_embedding_needed,
        "replay": False,
    }


async def execute_approved_candidate(
    session: Session, candidate_id: str, embed_client
) -> dict[str, Any]:
    """Execute one approved pair, with external embedding work before mutation."""
    plan = _prepare_merge(session, candidate_id, embed_client.model)
    if plan.replay:
        session.rollback()
        return {"status": "merged", "candidate_id": candidate_id, "replay": True}
    session.rollback()

    vector = None
    if plan.embedding_needed:
        vectors = await embed_client.embed_batch([plan.embedding_text])
        if len(vectors) != 1 or not vectors[0]:
            raise AliasError("embedding service returned no replacement vector")
        vector = vectors[0]
    for attempt in range(MAX_CONFLICT_RETRIES):
        try:
            return _apply_merge(session, plan, embed_client.model, vector)
        except DBAPIError as exc:
            session.rollback()
            if not _retryable_conflict(exc) or attempt + 1 == MAX_CONFLICT_RETRIES:
                raise
    raise AssertionError("unreachable")
