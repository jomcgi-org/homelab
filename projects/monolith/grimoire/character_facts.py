"""Viewer-scoped extraction input and transactional campaign fact writes.

No provider calls or scheduling live here. Callers own the transaction.
"""

import math
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ValidationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from grimoire.audience import audience_predicate
from grimoire.journal import journal, visible_rows
from grimoire.models import (
    ENTITY_DETAIL_MODELS,
    CampaignMember,
    CharacterFact,
    GameSession,
    KnowledgeGrant,
    Note,
    SessionEvent,
)
from grimoire.reveals import reveal_items
from grimoire.visibility import project_entity, visible_entities_query


class FactCandidate(BaseModel):
    statement: str
    evidence_event_ids: list[str]
    entity_id: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class FactRejection:
    index: int
    reason: str


@dataclass
class FactWriteResult:
    # Replayed rows are returned too, without changing their status or evidence.
    written: list[CharacterFact] = field(default_factory=list)
    rejections: list[FactRejection] = field(default_factory=list)


def _identity(value: str) -> str:
    return str(UUID(value))


def _viewer(session: Session, campaign_id: str, session_id: str, viewer_key: str):
    game = session.get(GameSession, session_id)
    if game is None or game.campaign_id != campaign_id:
        raise ValueError("session does not belong to campaign")
    if viewer_key == "party":
        # A party has no private author identity and sees only table events.
        return None, SimpleNamespace(
            id="party", role="player", player_character_id=None
        )
    member = session.exec(
        select(CampaignMember).where(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.role == "player",
            CampaignMember.player_character_id == viewer_key,
        )
    ).one_or_none()
    if member is None:
        raise ValueError("viewer must be a player character in this campaign or party")
    return member.player_character_id, member


def _events(session: Session, campaign_id: str, session_id: str, viewer, member):
    events = session.exec(
        select(SessionEvent)
        .where(
            SessionEvent.campaign_id == campaign_id,
            SessionEvent.session_id == session_id,
            SessionEvent.retracted_at.is_(None),
            audience_predicate(SessionEvent, viewer, member),
        )
        .order_by(SessionEvent.seq, SessionEvent.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    return visible_rows(viewer, member, events, "party" if viewer is None else "mine")


def _projections(session: Session, campaign_id: str, viewer):
    projections = {}
    for entity, grant in session.exec(
        visible_entities_query(campaign_id, viewer).execution_options(
            populate_existing=True
        )
    ).all():
        detail_model = ENTITY_DETAIL_MODELS.get(entity.entity_type)
        detail = (
            session.get(detail_model, entity.id, populate_existing=True)
            if detail_model
            else None
        )
        projection = project_entity(
            entity, detail, grant, viewer, context="relationship"
        )
        if projection is not None:
            projections[_identity(entity.id)] = projection
    return projections


def _vocabulary(session: Session, campaign_id: str, viewer):
    if viewer is not None:
        return _projections(session, campaign_id, viewer)
    # Party grounding is the common projection, not the union of private grants.
    viewers = session.exec(
        select(CampaignMember.player_character_id).where(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.role == "player",
            CampaignMember.player_character_id.is_not(None),
        )
    ).all()
    common = _projections(session, campaign_id, None) if not viewers else None
    for pc in viewers:
        projection = _projections(session, campaign_id, pc)
        if common is None:
            common = projection
        else:
            common = {
                entity_id: {
                    key: value
                    for key, value in fields.items()
                    if key in projection[entity_id]
                    and projection[entity_id][key] == value
                }
                for entity_id, fields in common.items()
                if entity_id in projection
            }
    return common or {}


def build_fact_payload(
    session: Session, campaign_id: str, session_id: str, viewer_key: str
) -> dict:
    """Narrow input built only from database-authorized viewer projections.

    #6625 and #6619 are not on main at implementation time. Delegate to the
    per-player prompt builder and utterances_for_context when they land; this
    function is intentionally not either feature's implementation.
    """
    viewer, member = _viewer(session, campaign_id, session_id, viewer_key)
    events = _events(session, campaign_id, session_id, viewer, member)
    vocabulary = _vocabulary(session, campaign_id, viewer)
    projected = []
    for row in events:
        if row.kind == "utterance" and row.body.get("ooc") is True:
            continue
        body = row.body
        if row.kind == "reveal":
            body = {
                "reveals": [
                    {
                        key: value
                        for key, value in item.items()
                        if key not in ("text", "entity", "projection")
                    }
                    if item.get("grant_scope") == "name_only"
                    else item
                    for item in reveal_items(body)
                    if not item.get("silent")
                ]
            }
        projected.append(
            SimpleNamespace(
                id=row.id,
                seq=row.seq,
                kind=row.kind,
                body=body,
                audience=row.audience,
                audience_pc_ids=row.audience_pc_ids,
                author_member_id=row.author_member_id,
                retracted_at=None,
            )
        )
    grants = set(
        session.exec(
            select(KnowledgeGrant.player_character_id, KnowledgeGrant.entity_id).where(
                KnowledgeGrant.campaign_id == campaign_id,
                KnowledgeGrant.player_character_id == viewer,
            )
        ).all()
    )
    folded = journal(
        viewer,
        member,
        projected,
        current_grants=grants,
        visible_entities=vocabulary,
        view="party" if viewer is None else "mine",
    )
    notes = session.exec(
        select(Note)
        .where(
            Note.campaign_id == campaign_id,
            Note.deleted_at.is_(None),
            Note.kind == ("party" if viewer is None else "character"),
            True if viewer is None else Note.author_member_id == member.id,
        )
        .order_by(Note.id)
        .execution_options(populate_existing=True)
    ).all()
    return jsonable_encoder(
        {
            "campaign_id": campaign_id,
            "session_id": session_id,
            "viewer_key": viewer_key,
            "events": [
                {"id": row.id, "seq": row.seq, "kind": row.kind, "body": row.body}
                for row in projected
                if row.kind != "utterance"
            ],
            "utterances": [
                {"id": row.id, "seq": row.seq, "body": row.body}
                for row in projected
                if row.kind == "utterance"
            ],
            "journal": asdict(folded),
            "notes": [
                {"id": row.id, "title": row.title, "markdown": row.markdown}
                for row in notes
            ],
            "entities": list(vocabulary.values()),
        }
    )


def write_character_facts(
    session: Session,
    campaign_id: str,
    session_id: str,
    viewer_key: str,
    extraction_version: str,
    candidates: list[FactCandidate | dict],
) -> FactWriteResult:
    """Validate each fact against fresh server state, then replay-safe insert.

    Lock evidence rows through commit so a concurrent event retraction either
    precedes this validation or cascades over the inserted facts afterwards.
    No caller-provided visibility sets are accepted. No commit occurs here.
    """
    result = FactWriteResult()
    viewer, member = _viewer(session, campaign_id, session_id, viewer_key)
    if not extraction_version.strip():
        raise ValueError("extraction_version must not be empty")
    if not candidates:
        return result
    events = {
        _identity(row.id): row.id
        for row in _events(session, campaign_id, session_id, viewer, member)
    }
    vocabulary = _vocabulary(session, campaign_id, viewer)
    accepted = {}
    for index, candidate in enumerate(candidates):
        try:
            fact = FactCandidate.model_validate(candidate)
            statement = fact.statement.strip()
            evidence = sorted({_identity(value) for value in fact.evidence_event_ids})
            entity = _identity(fact.entity_id) if fact.entity_id is not None else None
            if not statement:
                raise ValueError("statement must not be empty")
            if not evidence:
                raise ValueError("evidence must not be empty")
            if any(value not in events for value in evidence):
                raise ValueError(
                    "evidence is not live and visible in this session and campaign"
                )
            if entity is not None and entity not in vocabulary:
                raise ValueError("entity is outside the viewer's vocabulary")
            if fact.confidence is not None and (
                not math.isfinite(fact.confidence) or not 0 <= fact.confidence <= 1
            ):
                raise ValueError("confidence must be finite and between 0 and 1")
        except (ValueError, ValidationError, TypeError, AttributeError) as exc:
            result.rejections.append(FactRejection(index, str(exc)))
            continue
        accepted.setdefault(
            statement,
            {
                "id": str(uuid4()),
                "campaign_id": campaign_id,
                "session_id": session_id,
                "viewer_key": viewer or "party",
                "player_character_id": viewer,
                "statement": statement,
                "entity_id": vocabulary[entity]["id"] if entity is not None else None,
                "evidence_event_ids": [events[value] for value in evidence],
                "extraction_version": extraction_version,
                "status": "active",
            },
        )
    if accepted:
        insert = (
            sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
        )
        session.execute(
            insert(CharacterFact)
            .values(list(accepted.values()))
            .on_conflict_do_nothing(
                index_elements=[
                    "session_id",
                    "viewer_key",
                    "extraction_version",
                    "statement",
                ]
            )
        )
        result.written = list(
            session.exec(
                select(CharacterFact)
                .where(
                    CharacterFact.session_id == session_id,
                    CharacterFact.viewer_key == (viewer or "party"),
                    CharacterFact.extraction_version == extraction_version,
                    CharacterFact.statement.in_(accepted),
                )
                .order_by(CharacterFact.statement)
            ).all()
        )
    return result


def retract_facts_for_event(session: Session, event: SessionEvent) -> None:
    """Retract facts only when all supporting events are retracted, in this transaction."""
    from grimoire.play_embeddings import sync_fact_embeddings

    if event.retracted_at is None:
        return
    query = select(CharacterFact).where(
        CharacterFact.campaign_id == event.campaign_id,
        CharacterFact.session_id == event.session_id,
        CharacterFact.status != "retracted",
    )
    if session.get_bind().dialect.name == "postgresql":
        query = query.where(CharacterFact.evidence_event_ids.contains([event.id]))
    for fact in session.exec(query.order_by(CharacterFact.id).with_for_update()).all():
        if event.id not in fact.evidence_event_ids:
            continue
        surviving = session.exec(
            select(SessionEvent.id).where(
                SessionEvent.campaign_id == fact.campaign_id,
                SessionEvent.session_id == fact.session_id,
                SessionEvent.id.in_(fact.evidence_event_ids),
                SessionEvent.retracted_at.is_(None),
            )
        ).first()
        if surviving is None:
            fact.status = "retracted"
            sync_fact_embeddings(session, fact)
