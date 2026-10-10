"""Grimoire campaign CRUD + grant-filtered entity read HTTP API (private tier).

Covers campaigns, player characters, knowledge grants (the DM's visibility
overlay), game sessions, and the grant-filtered entity/relationship read
paths. Vector search lives in a later module (search.py).

CRUD handlers return a Pydantic response model, never a SQLModel table
object (ADR 010: keep the DB row shape off the wire). The entity read paths
are the exception: their projected shape is scope-dependent (full spine,
partial identity + revealed_details, or a DM view with grant annotations),
so they return plain dicts, matching the heterogeneous-payload pattern
already used elsewhere (e.g. knowledge/router.py's `-> dict` handlers).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from auth.api import Authority, Principal, PrincipalKind, get_principal
from core.db import get_session
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from knowledge.api import get_embedding_client
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from shared.embedding import EmbeddingClient
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, or_, select

from grimoire import aliases, library
from grimoire.access import (
    get_authenticated_email,
    get_authenticated_identity,
    get_game_creator_email,
    get_grimoire_operator_email,
)
from grimoire.accounts import find_registered_user
from grimoire.audience import (
    Audience,
    AudienceKind,
    audience_predicate,
    inventory_predicate,
    note_predicate,
)
from grimoire.dice import DiceFormulaError, DiceRng, get_dice_rng, roll
from grimoire.invitation_provider import enrollment_enabled
from grimoire.join_links import links_enabled
from grimoire.join_links import router as join_links_router
from grimoire.journal import Journal, journal, narration_entity_ids, visible_rows
from grimoire.models import (
    ENTITY_DETAIL_MODELS,
    AppUser,
    Campaign,
    CampaignInvitation,
    CampaignJoinLink,
    CampaignMember,
    CharacterSheetStatus,
    CharacterSheetVersion,
    Entity,
    EntityType,
    EventKind,
    GameSession,
    GrantScope,
    InventoryChange,
    InventoryItem,
    KnowledgeChunk,
    KnowledgeGrant,
    MemberRole,
    Note,
    PlayerCharacter,
    Relationship,
    SessionEvent,
    SessionStatus,
)
from grimoire.play_embeddings import (
    event_note_markdown as _event_note_markdown,
)
from grimoire.play_embeddings import (
    sync_event_embeddings,
    sync_note_embeddings,
)
from grimoire.reveals import reveal_items
from grimoire.search import search_campaign, search_knowledge
from grimoire.session_events import (
    EventRequestConflictError,
    InvalidEventAudienceError,
    SessionEndedError,
    append_event,
    play_enabled,
    require_play_enabled,
)
from grimoire.sheets import CharacterSheetV1, SheetValidationError, derive_sheet
from grimoire.visibility import (
    Viewer,
    entity_belongs_to_campaign,
    project_entity,
    visible_entities_query,
)

logger = logging.getLogger("monolith.grimoire.router")

router = APIRouter(prefix="/api/grimoire", tags=["grimoire"])


# --- Alias review ------------------------------------------------------


class AliasApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    survivor_entity_id: str
    expected_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class AliasDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _alias_operator(principal: Principal = Depends(get_principal)) -> Principal:
    if (
        principal.authority is not Authority.STANDING
        or principal.kind is not PrincipalKind.HUMAN
        or not principal.has_group("operators")
    ):
        raise HTTPException(status_code=403, detail="operator access required")
    return principal


def _alias_http_error(exc: aliases.AliasError) -> HTTPException:
    status_code = 404 if isinstance(exc, aliases.AliasNotFound) else 409
    return HTTPException(status_code=status_code, detail=str(exc))


@router.post("/alias-candidates/scan")
def scan_alias_candidates(
    session: Session = Depends(get_session),
    _principal: Principal = Depends(_alias_operator),
) -> dict[str, Any]:
    """Refresh and publish the conservative candidate report for human review."""
    return aliases.generate_candidates(session)


@router.get("/alias-candidates")
def get_alias_candidates(
    status: Literal["pending", "approved", "rejected", "stale", "merged"] | None = None,
    session: Session = Depends(get_session),
    _principal: Principal = Depends(_alias_operator),
) -> list[dict[str, Any]]:
    """List durable candidates with bounded co-mention snippets and approvals."""
    return aliases.list_candidates(session, status=status)


@router.post("/alias-candidates/{candidate_id}/approve")
def approve_alias_candidate(
    candidate_id: str,
    body: AliasApprovalRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(_alias_operator),
) -> dict[str, Any]:
    """Record explicit reviewer approval for the exact current candidate state."""
    try:
        return aliases.approve_candidate(
            session,
            candidate_id,
            reviewer=principal.subject,
            survivor_id=body.survivor_entity_id,
            expected_state_hash=body.expected_state_hash,
        )
    except aliases.AliasError as exc:
        raise _alias_http_error(exc) from exc


@router.post("/alias-candidates/{candidate_id}/reject")
def reject_alias_candidate(
    candidate_id: str,
    body: AliasDecisionRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(_alias_operator),
) -> dict[str, Any]:
    """Persist a human rejection for the exact reviewed candidate state."""
    try:
        return aliases.reject_candidate(
            session,
            candidate_id,
            reviewer=principal.subject,
            expected_state_hash=body.expected_state_hash,
        )
    except aliases.AliasError as exc:
        raise _alias_http_error(exc) from exc


@router.post("/alias-candidates/{candidate_id}/reopen")
def reopen_alias_candidate(
    candidate_id: str,
    body: AliasDecisionRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(_alias_operator),
) -> dict[str, Any]:
    """Deliberately return a rejected or stale candidate to pending review."""
    try:
        return aliases.reopen_candidate(
            session,
            candidate_id,
            reviewer=principal.subject,
            expected_state_hash=body.expected_state_hash,
        )
    except aliases.AliasError as exc:
        raise _alias_http_error(exc) from exc


@router.post("/alias-candidates/{candidate_id}/execute")
async def execute_alias_candidate(
    candidate_id: str,
    session: Session = Depends(get_session),
    embed_client: EmbeddingClient = Depends(get_embedding_client),
    _principal: Principal = Depends(_alias_operator),
) -> dict[str, Any]:
    """Transactionally execute one still-current, explicitly approved pair."""
    try:
        return await aliases.execute_approved_candidate(
            session, candidate_id, embed_client
        )
    except aliases.AliasError as exc:
        raise _alias_http_error(exc) from exc


# --- Campaigns --------------------------------------------------------


class CampaignCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    dm_name: str | None = Field(default=None, max_length=120)


class CampaignView(BaseModel):
    id: str
    name: str
    dm_name: str | None
    created_at: datetime
    notes_dm_readable_default: bool = False


def _get_campaign_or_404(session: Session, campaign_id: str) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return campaign


def _get_member_or_404(
    session: Session, campaign_id: str, email: str
) -> CampaignMember:
    """Read current membership without revealing whether the campaign exists."""
    member = session.exec(
        select(CampaignMember)
        .join(AppUser, AppUser.id == CampaignMember.app_user_id)
        .where(
            CampaignMember.campaign_id == campaign_id,
            AppUser.id == session.info["grimoire_user_id"]
            if "grimoire_user_id" in session.info
            else AppUser.email == email,
        )
    ).first()
    if member is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return member


def _require_dm(session: Session, campaign_id: str, email: str) -> CampaignMember:
    member = _get_member_or_404(session, campaign_id, email)
    if member.role != "dm":
        raise HTTPException(status_code=403, detail="campaign DM role required")
    return member


def _require_owner(session: Session, campaign_id: str, email: str) -> AppUser:
    _get_member_or_404(session, campaign_id, email)
    campaign = _get_campaign_or_404(session, campaign_id)
    user = _request_user(session, email)
    if user is None or campaign.owner_app_user_id != user.id:
        raise HTTPException(403, detail="campaign owner required")
    return user


def _viewer_for_member(
    session: Session, campaign_id: str, member: CampaignMember
) -> Viewer:
    if member.role == "dm":
        return "dm"
    if member.player_character_id is None:
        return None
    _get_character_in_campaign_or_404(session, campaign_id, member.player_character_id)
    return member.player_character_id


def _request_user(session: Session, email: str) -> AppUser | None:
    user_id = session.info.get("grimoire_user_id")
    if user_id is not None:
        return session.get(AppUser, user_id)
    # Compatibility for the existing dependency seam and legacy operator jobs.
    return session.exec(select(AppUser).where(AppUser.email == email)).first()


def _get_or_create_user(session: Session, email: str) -> AppUser:
    user = session.exec(select(AppUser).where(AppUser.email == email)).first()
    if user is None:
        user = AppUser(email=email)
        session.add(user)
        session.flush()
    return user


@router.post("/campaigns", response_model=CampaignView)
def create_campaign(
    body: CampaignCreateRequest,
    email: str = Depends(get_game_creator_email),
    session: Session = Depends(get_session),
) -> Campaign:
    user = _request_user(session, email) or _get_or_create_user(session, email)
    campaign = Campaign(name=body.name, dm_name=body.dm_name, owner_app_user_id=user.id)
    session.add(campaign)
    session.flush()
    session.add(
        CampaignMember(
            campaign_id=campaign.id,
            app_user_id=user.id,
            role="dm",
        )
    )
    session.commit()
    session.refresh(campaign)
    return campaign


@router.get("/campaigns", response_model=list[CampaignView])
def list_campaigns(
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[Campaign]:
    return session.exec(
        select(Campaign)
        .join(CampaignMember, CampaignMember.campaign_id == Campaign.id)
        .join(AppUser, AppUser.id == CampaignMember.app_user_id)
        .where(
            AppUser.id == session.info["grimoire_user_id"]
            if "grimoire_user_id" in session.info
            else AppUser.email == email
        )
        .order_by(Campaign.created_at)
    ).all()


@router.get("/campaigns/{campaign_id}", response_model=CampaignView)
def get_campaign(
    campaign_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> Campaign:
    _get_member_or_404(session, campaign_id, email)
    return _get_campaign_or_404(session, campaign_id)


# --- Player and party notes --------------------------------------------


class NoteLinks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)

    @field_validator("entity_ids", "event_ids")
    @classmethod
    def bounded_unique(cls, values):
        # Retain persisted spelling for SQLite fixtures; PostgreSQL UUIDs
        # canonicalize on storage. Deduplicate UUID identity, including case.
        unique = {}
        for value in values:
            unique.setdefault(str(UUID(value)), value)
        values = list(unique.values())
        if len(values) > 50:
            raise ValueError("At most 50 links of each kind")
        return values

    def stored(self) -> dict:
        return {
            "entity_ids": [str(v) for v in self.entity_ids],
            "event_ids": [str(v) for v in self.event_ids],
        }


class NoteCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["character", "party"]
    title: str = Field(default="", max_length=200)
    markdown: str = Field(default="", max_length=20000)
    dm_readable: bool | None = None
    links: NoteLinks = Field(default_factory=NoteLinks)
    pinned: bool = False
    created_in_session: str | None = None
    from_event_id: UUID | None = None

    @model_validator(mode="after")
    def title_or_event(self):
        if not self.title and self.from_event_id is None:
            raise ValueError("Choose a note title")
        return self

    @field_validator("created_in_session")
    @classmethod
    def session_uuid(cls, value):
        if value is not None:
            UUID(value)
        return value

    @field_validator("dm_readable")
    @classmethod
    def readable_not_null(cls, value):
        if value is None:
            raise ValueError("dm_readable must be a boolean")
        return value


class NotePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    markdown: str | None = Field(default=None, max_length=20000)
    pinned: bool | None = None
    dm_readable: bool | None = None
    links: NoteLinks | None = None

    @field_validator("title", "markdown", "pinned", "dm_readable", "links")
    @classmethod
    def fields_not_null(cls, value):
        if value is None:
            raise ValueError("Note fields cannot be null")
        return value


def _note_entities(
    session: Session, campaign_id: str, viewer: Viewer, ids: list
) -> list[dict]:
    if not ids or viewer is None:
        return []
    entities = {}
    for entity, grant in session.exec(
        visible_entities_query(campaign_id, viewer).where(Entity.id.in_(ids))
    ).all():
        # Unknown grants fail closed. Chips are relationship-context identity,
        # so name-only recognition is permitted without any typed details.
        if grant is not None and grant.grant_scope not in (
            "full",
            "partial",
            "name_only",
        ):
            continue
        if not entity_belongs_to_campaign(session, campaign_id, entity):
            continue
        projected = project_entity(entity, None, grant, viewer, context="relationship")
        if projected is not None:
            entities[str(UUID(entity.id))] = {
                "id": entity.id,
                "name": projected["name"],
                "type": projected["entity_type"],
            }
    return [
        entities[str(UUID(entity_id))]
        for entity_id in ids
        if str(UUID(entity_id)) in entities
    ]


def _validate_note_links(
    session: Session, campaign_id: str, viewer: Viewer, links: NoteLinks
):
    ids = [str(value) for value in links.entity_ids]
    visible = _note_entities(session, campaign_id, viewer, ids)
    if len(visible) != len(ids):
        raise HTTPException(404, detail="entity not found")


def _note_view(
    session: Session, row: Note, viewer: Viewer, member: CampaignMember
) -> dict:
    mine = row.author_member_id is not None and row.author_member_id == member.id

    # Normalize SQLite's naive TIMESTAMPTZ mirror to the PostgreSQL wire shape.
    def iso(value):
        return (
            value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        ).isoformat()

    result = {
        "id": row.id,
        "title": row.title,
        "markdown": row.markdown,
        "kind": row.kind,
        "pinned": row.pinned,
        "created_in_session": row.created_in_session,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
        "is_mine": mine,
        "can_edit": mine or (row.kind == "party" and member.role == "dm"),
        "links": {
            "entities": _note_entities(
                session, row.campaign_id, viewer, row.links["entity_ids"]
            ),
            # Opaque provenance only. Do not read a feed event from these ids.
            "event_ids": row.links["event_ids"],
        },
    }
    if mine:
        result["dm_readable"] = row.dm_readable
    if mine or viewer == "dm":
        result["author_member_id"] = row.author_member_id
        result["player_character_id"] = row.player_character_id
    return result


def _get_note_or_404(
    session: Session,
    campaign_id: str,
    note_id: str,
    viewer: Viewer,
    member: CampaignMember,
    *,
    lock=False,
) -> Note:
    query = select(Note).where(
        Note.campaign_id == campaign_id,
        Note.id == note_id,
        note_predicate(Note, viewer, member),
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = session.exec(query).one_or_none()
    if row is None:
        raise HTTPException(404, detail="note not found")
    return row


def _require_note_editor(row: Note, member: CampaignMember):
    if row.author_member_id == member.id or (
        row.kind == "party" and member.role == "dm"
    ):
        return
    raise HTTPException(403, detail="note edit not permitted")


@router.get("/campaigns/{campaign_id}/notes")
def list_notes(
    campaign_id: str,
    kind: Literal["character", "party"] | None = None,
    q: str = Query(default="", max_length=20000),
    limit: int = Query(default=100, ge=1, le=500),
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[dict]:
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    query = select(Note).where(
        Note.campaign_id == campaign_id, note_predicate(Note, viewer, member)
    )
    if kind is not None:
        query = query.where(Note.kind == kind)
    if q:
        pattern = (
            "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        query = query.where(
            or_(
                func.lower(Note.title).like(func.lower(pattern), escape="\\"),
                func.lower(Note.markdown).like(func.lower(pattern), escape="\\"),
            )
        )
    rows = session.exec(
        query.order_by(Note.pinned.desc(), Note.updated_at.desc(), Note.id).limit(limit)
    ).all()
    return [_note_view(session, row, viewer, member) for row in rows]


@router.post("/campaigns/{campaign_id}/notes")
def create_note(
    campaign_id: str,
    body: NoteCreateRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> dict:
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    if viewer is None or (viewer == "dm" and body.kind != "party"):
        raise HTTPException(403, detail="note creation not permitted")
    campaign = _get_campaign_or_404(session, campaign_id)
    if body.from_event_id is not None:
        event = session.exec(
            select(SessionEvent).where(
                SessionEvent.id == str(body.from_event_id),
                SessionEvent.campaign_id == campaign_id,
                audience_predicate(SessionEvent, viewer, member),
            )
        ).first()
        if event is None or event.retracted_at is not None:
            raise HTTPException(404, detail="event not found")
        projection = _event_view(event, member).body
        if not projection or projection.get("retracted"):
            raise HTTPException(404, detail="event not found")
        body.markdown = _event_note_markdown(projection)
        items = reveal_items(projection)
        body.title = (
            projection.get("name")
            or ", ".join(item["name"] for item in items)
            or body.title
            or "Session note"
        )
        body.links = NoteLinks(
            entity_ids=[item["entity_id"] for item in reveal_items(projection)],
            event_ids=[event.id],
        )
        body.created_in_session = event.session_id
        body.pinned = True
    if body.created_in_session is not None:
        if (
            session.exec(
                select(GameSession.id).where(
                    GameSession.id == str(body.created_in_session),
                    GameSession.campaign_id == campaign_id,
                )
            ).first()
            is None
        ):
            raise HTTPException(404, detail="session not found")
    _validate_note_links(session, campaign_id, viewer, body.links)
    now = datetime.now(timezone.utc)
    row = Note(
        campaign_id=campaign_id,
        author_member_id=member.id,
        player_character_id=member.player_character_id,
        kind=body.kind,
        title=body.title,
        markdown=body.markdown,
        pinned=body.pinned,
        dm_readable=campaign.notes_dm_readable_default
        if body.dm_readable is None
        else body.dm_readable,
        links=body.links.stored(),
        created_in_session=str(body.created_in_session)
        if body.created_in_session
        else None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _note_view(session, row, viewer, member)


@router.get("/campaigns/{campaign_id}/notes/{note_id}")
def get_note(
    campaign_id: str,
    note_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> dict:
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    row = _get_note_or_404(session, campaign_id, note_id, viewer, member)
    return _note_view(session, row, viewer, member)


@router.patch("/campaigns/{campaign_id}/notes/{note_id}")
def patch_note(
    campaign_id: str,
    note_id: str,
    body: NotePatchRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> dict:
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    row = _get_note_or_404(session, campaign_id, note_id, viewer, member, lock=True)
    _require_note_editor(row, member)
    changes = body.model_dump(exclude_unset=True, exclude={"links"})
    if "dm_readable" in changes and (
        row.kind != "character" or row.author_member_id != member.id
    ):
        raise HTTPException(403, detail="note sharing change not permitted")
    if body.links is not None:
        _validate_note_links(session, campaign_id, viewer, body.links)
        row.links = body.links.stored()
    for field, value in changes.items():
        setattr(row, field, value)
    row.updated_at = datetime.now(timezone.utc)
    sync_note_embeddings(session, row)
    session.commit()
    session.refresh(row)
    return _note_view(session, row, viewer, member)


@router.delete("/campaigns/{campaign_id}/notes/{note_id}", status_code=204)
def delete_note(
    campaign_id: str,
    note_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
):
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    row = _get_note_or_404(session, campaign_id, note_id, viewer, member, lock=True)
    _require_note_editor(row, member)
    row.deleted_at = row.updated_at = datetime.now(timezone.utc)
    sync_note_embeddings(session, row)
    session.commit()


# --- Inventory --------------------------------------------------------


class InventoryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str
    name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(default=1, ge=1, le=1000000)
    notes: str = Field(default="", max_length=20000)
    entity_id: str | None = None
    hidden_from_party: bool = False
    reason: str = Field(default="", max_length=500)
    source_event_id: str | None = None

    @field_validator("entity_id", "source_event_id")
    @classmethod
    def link_uuid(cls, value):
        if value is not None:
            UUID(value)
        return value


class InventoryPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=20000)
    entity_id: str | None = None
    hidden_from_party: bool | None = None
    quantity: int | None = Field(default=None, ge=0, le=1000000)
    reason: str = Field(default="", max_length=500)

    @field_validator("name", "notes", "entity_id", "hidden_from_party", "quantity")
    @classmethod
    def fields_not_null(cls, value):
        if value is None:
            raise ValueError("Inventory fields cannot be null")
        return value

    @field_validator("entity_id")
    @classmethod
    def entity_uuid(cls, value):
        UUID(value)
        return value


class InventoryMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str
    quantity: int | None = Field(default=None, ge=1, le=1000000)
    reason: str = Field(default="", max_length=500)

    @field_validator("quantity")
    @classmethod
    def quantity_not_null(cls, value):
        if value is None:
            raise ValueError("Move quantity cannot be null")
        return value


def _inventory_iso(value):
    return (
        (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value)
        .astimezone(timezone.utc)
        .isoformat()
    )


def _inventory_owner(row):
    return "party" if row.owner_kind == "party" else row.player_character_id


def _inventory_destination(session, campaign_id, owner):
    if owner == "party":
        return "party", None
    try:
        UUID(owner)
    except ValueError as exc:
        raise HTTPException(
            404, detail="player character not found in this campaign"
        ) from exc
    pc = _get_character_in_campaign_or_404(session, campaign_id, owner)
    return "character", pc.id


def _inventory_view(session, row, viewer):
    entities = _note_entities(
        session, row.campaign_id, viewer, [row.entity_id] if row.entity_id else []
    )
    return {
        "id": row.id,
        "owner": _inventory_owner(row),
        "is_mine": viewer not in (None, "dm") and row.player_character_id == viewer,
        "name": row.name,
        "quantity": row.quantity,
        "notes": row.notes,
        "hidden_from_party": row.hidden_from_party,
        "entity": entities[0] if entities else None,
        "created_at": _inventory_iso(row.created_at),
        "updated_at": _inventory_iso(row.updated_at),
    }


def _get_inventory_or_404(session, campaign_id, item_id, viewer, *, lock=False):
    query = select(InventoryItem).where(
        InventoryItem.campaign_id == campaign_id,
        InventoryItem.id == item_id,
        inventory_predicate(InventoryItem, viewer),
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = session.exec(query).one_or_none()
    if row is None:
        raise HTTPException(404, detail="item not found")
    return row


def _inventory_change(row, member, action, delta, reason, changes):
    return InventoryChange(
        campaign_id=row.campaign_id,
        item_id=row.id,
        who_member_id=member.id,
        action=action,
        delta=delta,
        quantity_after=row.quantity,
        reason=reason,
        changes=changes,
    )


def _record_inventory(session, row, member, changes):
    """Flush the event before inserting audit rows, never update an audit row."""
    current = _current_reveal_session(session, row.campaign_id)
    event = None
    if current is not None:
        if not row.hidden_from_party:
            owner = (
                "the party pool"
                if row.owner_kind == "party"
                else "a character's inventory"
            )
            try:
                event = append_event(
                    session,
                    game_session=current,
                    kind="system",
                    audience=Audience("table", frozenset(), author_member_id=member.id),
                    author_member_id=member.id,
                    body={
                        "text": f"Inventory changed in {owner}.",
                        "inventory_change_ids": [change.id for change in changes],
                        "action": changes[0].action,
                    },
                )
            except SessionEndedError as exc:
                session.rollback()
                raise HTTPException(409, detail=str(exc)) from exc
            except Exception:
                session.rollback()
                raise
        for change in changes:
            change.session_id = current.id
            change.event_id = event.id if event else None
    session.add_all(changes)
    session.commit()


def _inventory_change_view(session, row, viewer, member):
    changes = dict(row.changes)
    if viewer != "dm":
        if "owner" in changes:
            changes["owner"] = {
                key: "party"
                if value == "party"
                else "you"
                if value == viewer
                else "character"
                for key, value in changes["owner"].items()
            }
        # Audit edits must not bypass entity/event audience projections.
        if "entity_id" in changes:
            changes["entity_id"] = {
                key: value
                if value and _note_entities(session, row.campaign_id, viewer, [value])
                else None
                for key, value in changes["entity_id"].items()
            }
        if "source_event_id" in changes:
            visible = session.exec(
                select(SessionEvent.id).where(
                    SessionEvent.id == changes["source_event_id"],
                    SessionEvent.campaign_id == row.campaign_id,
                    audience_predicate(SessionEvent, viewer, member),
                )
            ).first()
            if visible is None:
                changes.pop("source_event_id")
    result = {
        "id": row.id,
        "item_id": row.item_id,
        "action": row.action,
        "delta": row.delta,
        "quantity_after": row.quantity_after,
        "reason": row.reason,
        "session_id": row.session_id,
        "event_id": row.event_id,
        "created_at": _inventory_iso(row.created_at),
        "is_mine": row.who_member_id is not None and row.who_member_id == member.id,
        "changes": changes,
    }
    if viewer == "dm":
        result["who_member_id"] = row.who_member_id
    return result


@router.get("/campaigns/{campaign_id}/inventory")
def list_inventory(
    campaign_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[dict]:
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    rows = session.exec(
        select(InventoryItem)
        .where(
            InventoryItem.campaign_id == campaign_id,
            inventory_predicate(InventoryItem, viewer),
        )
        .order_by(
            (InventoryItem.owner_kind == "party").desc(),
            InventoryItem.player_character_id,
            InventoryItem.name,
            InventoryItem.id,
        )
    ).all()
    return [_inventory_view(session, row, viewer) for row in rows]


@router.get("/campaigns/{campaign_id}/inventory/changes")
def list_inventory_changes(
    campaign_id: str,
    item_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[dict]:
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    query = (
        select(InventoryChange)
        .join(InventoryItem, InventoryItem.id == InventoryChange.item_id)
        .where(
            InventoryChange.campaign_id == campaign_id,
            InventoryItem.campaign_id == campaign_id,
        )
    )
    if viewer != "dm":
        query = query.where(inventory_predicate(InventoryItem, viewer))
    if item_id is not None:
        try:
            UUID(item_id)
        except ValueError as exc:
            raise HTTPException(422, detail="invalid item id") from exc
        # A requested invisible item is indistinguishable from a missing one.
        if viewer != "dm":
            _get_inventory_or_404(session, campaign_id, str(item_id), viewer)
        query = query.where(InventoryChange.item_id == str(item_id))
    rows = session.exec(
        query.order_by(
            InventoryChange.created_at.desc(), InventoryChange.id.desc()
        ).limit(limit)
    ).all()
    return [_inventory_change_view(session, row, viewer, member) for row in rows]


@router.post("/campaigns/{campaign_id}/inventory")
def create_inventory(
    campaign_id: str,
    body: InventoryCreateRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> dict:
    member = _require_dm(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    kind, pc_id = _inventory_destination(session, campaign_id, body.owner)
    if body.entity_id is not None:
        _validate_note_links(
            session, campaign_id, viewer, NoteLinks(entity_ids=[body.entity_id])
        )
    changes = {}
    if body.source_event_id is not None:
        event = session.exec(
            select(SessionEvent).where(
                SessionEvent.id == str(body.source_event_id),
                SessionEvent.campaign_id == campaign_id,
            )
        ).first()
        if event is None:
            raise HTTPException(404, detail="event not found")
        changes["source_event_id"] = event.id
    row = InventoryItem(
        campaign_id=campaign_id,
        owner_kind=kind,
        player_character_id=pc_id,
        name=body.name,
        quantity=body.quantity,
        notes=body.notes,
        entity_id=str(body.entity_id) if body.entity_id else None,
        hidden_from_party=body.hidden_from_party,
    )
    session.add(row)
    _record_inventory(
        session,
        row,
        member,
        [_inventory_change(row, member, "create", row.quantity, body.reason, changes)],
    )
    session.refresh(row)
    return _inventory_view(session, row, viewer)


@router.patch("/campaigns/{campaign_id}/inventory/{item_id}")
def patch_inventory(
    campaign_id: str,
    item_id: str,
    body: InventoryPatchRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> dict:
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    row = _get_inventory_or_404(session, campaign_id, item_id, viewer, lock=True)
    fields = body.model_dump(exclude_unset=True, exclude={"reason"})
    if viewer != "dm" and (
        row.player_character_id != viewer or set(fields) - {"quantity"}
    ):
        raise HTTPException(403, detail="inventory edit not permitted")
    if body.entity_id is not None:
        _validate_note_links(
            session, campaign_id, viewer, NoteLinks(entity_ids=[body.entity_id])
        )
        fields["entity_id"] = str(body.entity_id)
    old_quantity = row.quantity
    changes = {
        key: {"from": getattr(row, key), "to": value} for key, value in fields.items()
    }
    for key, value in fields.items():
        setattr(row, key, value)
    row.updated_at = datetime.now(timezone.utc)
    _record_inventory(
        session,
        row,
        member,
        [
            _inventory_change(
                row, member, "update", row.quantity - old_quantity, body.reason, changes
            )
        ],
    )
    session.refresh(row)
    return _inventory_view(session, row, viewer)


@router.post("/campaigns/{campaign_id}/inventory/{item_id}/move")
def move_inventory(
    campaign_id: str,
    item_id: str,
    body: InventoryMoveRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> dict:
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    row = _get_inventory_or_404(session, campaign_id, item_id, viewer, lock=True)
    if viewer != "dm" and not (
        not row.hidden_from_party
        and (
            row.player_character_id == viewer
            and body.owner == "party"
            or row.owner_kind == "party"
            and body.owner == viewer
        )
    ):
        raise HTTPException(403, detail="inventory move not permitted")
    kind, pc_id = _inventory_destination(session, campaign_id, body.owner)
    quantity = row.quantity if body.quantity is None else body.quantity
    if not 0 < quantity <= row.quantity:
        raise HTTPException(422, detail="move quantity exceeds available inventory")
    changes = {
        "owner": {
            "from": _inventory_owner(row),
            "to": "party" if kind == "party" else pc_id,
        }
    }
    row.updated_at = datetime.now(timezone.utc)
    if quantity == row.quantity:
        row.owner_kind, row.player_character_id = kind, pc_id
        destination = row
        audit = [_inventory_change(row, member, "move", 0, body.reason, changes)]
    else:
        row.quantity -= quantity
        destination = InventoryItem(
            campaign_id=campaign_id,
            owner_kind=kind,
            player_character_id=pc_id,
            name=row.name,
            quantity=quantity,
            notes=row.notes,
            entity_id=row.entity_id,
            hidden_from_party=row.hidden_from_party,
        )
        session.add_all([row, destination])
        audit = [
            _inventory_change(row, member, "move", -quantity, body.reason, changes),
            _inventory_change(
                destination, member, "move", quantity, body.reason, changes
            ),
        ]
    _record_inventory(session, destination, member, audit)
    session.refresh(destination)
    return _inventory_view(session, destination, viewer)


@router.delete("/campaigns/{campaign_id}/inventory/{item_id}", status_code=204)
def delete_inventory(
    campaign_id: str,
    item_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
):
    member = _require_dm(session, campaign_id, email)
    row = _get_inventory_or_404(session, campaign_id, item_id, "dm", lock=True)
    row.deleted_at = row.updated_at = datetime.now(timezone.utc)
    _record_inventory(
        session, row, member, [_inventory_change(row, member, "delete", 0, "", {})]
    )


class CampaignSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes_dm_readable_default: bool


@router.patch("/campaigns/{campaign_id}/settings", response_model=CampaignView)
def patch_campaign_settings(
    campaign_id: str,
    body: CampaignSettingsRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> Campaign:
    _require_dm(session, campaign_id, email)
    campaign = _get_campaign_or_404(session, campaign_id)
    campaign.notes_dm_readable_default = body.notes_dm_readable_default
    session.commit()
    session.refresh(campaign)
    return campaign


# --- Player characters --------------------------------------------------


class CharacterCreateRequest(BaseModel):
    character_name: str
    player_name: str | None = None
    class_name: str | None = None
    level: int | None = None
    sheet: dict = {}


class CharacterView(BaseModel):
    id: str
    campaign_id: str
    player_name: str | None
    character_name: str
    class_name: str | None
    level: int | None
    sheet: dict


class CharacterNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)


@router.post(
    "/campaigns/{campaign_id}/characters",
    response_model=CharacterView,
)
def create_character(
    campaign_id: str,
    body: CharacterCreateRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> PlayerCharacter:
    _require_dm(session, campaign_id, email)
    character = PlayerCharacter(
        campaign_id=campaign_id,
        character_name=body.character_name,
        player_name=body.player_name,
        class_name=body.class_name,
        level=body.level,
        sheet=body.sheet,
    )
    session.add(character)
    session.commit()
    session.refresh(character)
    return character


@router.post(
    "/campaigns/{campaign_id}/characters/self",
    response_model=CharacterView,
)
def create_own_character(
    campaign_id: str,
    body: CharacterNameRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> PlayerCharacter:
    member = _get_member_or_404(session, campaign_id, email)
    if member.role != "player":
        raise HTTPException(status_code=403, detail="campaign player role required")
    # Refresh after the lock: another request may have assigned this member.
    member = session.exec(
        select(CampaignMember)
        .where(CampaignMember.id == member.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if member.player_character_id is not None:
        raise HTTPException(status_code=409, detail="player already has a character")
    character = PlayerCharacter(campaign_id=campaign_id, character_name=body.name)
    session.add(character)
    session.flush()
    member.player_character_id = character.id
    session.add(member)
    session.commit()
    session.refresh(character)
    return character


@router.get(
    "/campaigns/{campaign_id}/characters",
    response_model=list[CharacterView],
)
def list_characters(
    campaign_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[PlayerCharacter]:
    member = _get_member_or_404(session, campaign_id, email)
    query = select(PlayerCharacter).where(PlayerCharacter.campaign_id == campaign_id)
    if member.role != "dm":
        if member.player_character_id is None:
            return []
        query = query.where(PlayerCharacter.id == member.player_character_id)
    return session.exec(query.order_by(PlayerCharacter.character_name)).all()


# --- Versioned character sheets ----------------------------------------


class CharacterSheetView(BaseModel):
    id: str
    campaign_id: str
    player_character_id: str
    version: int
    contract_version: int
    status: CharacterSheetStatus
    sheet: dict
    derived: dict
    created_by_email: str
    submitted_at: datetime | None
    decided_at: datetime | None
    decision_comment: str | None
    decided_by_email: str | None
    created_at: datetime
    updated_at: datetime


class CharacterSheetHistoryView(BaseModel):
    character: CharacterView
    viewer_role: MemberRole
    versions: list[CharacterSheetView]


class SheetDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(default=None, max_length=1000)

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None


def _require_character_reader(
    session: Session,
    campaign_id: str,
    player_character_id: str,
    email: str,
) -> tuple[CampaignMember, PlayerCharacter]:
    member = _get_member_or_404(session, campaign_id, email)
    character = _get_character_in_campaign_or_404(
        session, campaign_id, player_character_id
    )
    if member.role == "player" and member.player_character_id != character.id:
        raise HTTPException(status_code=404, detail="player character not found")
    return member, character


def _require_character_owner(
    session: Session,
    campaign_id: str,
    player_character_id: str,
    email: str,
) -> tuple[CampaignMember, PlayerCharacter]:
    member, character = _require_character_reader(
        session, campaign_id, player_character_id, email
    )
    if member.role != "player":
        raise HTTPException(status_code=403, detail="player role required")
    if member.player_character_id != character.id:
        raise HTTPException(status_code=404, detail="player character not found")
    return member, character


def _get_sheet_version_or_404(
    session: Session,
    campaign_id: str,
    player_character_id: str,
    sheet_version_id: str,
) -> CharacterSheetVersion:
    version = session.get(CharacterSheetVersion, sheet_version_id)
    if (
        version is None
        or version.campaign_id != campaign_id
        or version.player_character_id != player_character_id
    ):
        raise HTTPException(status_code=404, detail="character sheet version not found")
    return version


def _derive_sheet_or_422(
    session: Session,
    campaign_id: str,
    player_character_id: str,
    sheet: CharacterSheetV1,
) -> dict:
    try:
        return derive_sheet(session, campaign_id, player_character_id, sheet)
    except SheetValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/campaigns/{campaign_id}/characters/{player_character_id}/sheets",
    response_model=CharacterSheetHistoryView,
)
def list_character_sheet_versions(
    campaign_id: str,
    player_character_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> CharacterSheetHistoryView:
    member, character = _require_character_reader(
        session, campaign_id, player_character_id, email
    )
    versions = session.exec(
        select(CharacterSheetVersion)
        .where(
            CharacterSheetVersion.campaign_id == campaign_id,
            CharacterSheetVersion.player_character_id == player_character_id,
        )
        .order_by(CharacterSheetVersion.version.desc())
    ).all()
    return CharacterSheetHistoryView(
        character=CharacterView.model_validate(character, from_attributes=True),
        viewer_role=member.role,
        versions=[
            CharacterSheetView.model_validate(version, from_attributes=True)
            for version in versions
        ],
    )


@router.post(
    "/campaigns/{campaign_id}/characters/{player_character_id}/sheets/drafts",
    response_model=CharacterSheetView,
)
def create_character_sheet_draft(
    campaign_id: str,
    player_character_id: str,
    body: CharacterSheetV1,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> CharacterSheetVersion:
    _require_character_owner(session, campaign_id, player_character_id, email)
    session.exec(
        select(PlayerCharacter)
        .where(PlayerCharacter.id == player_character_id)
        .with_for_update()
    ).first()
    latest = session.exec(
        select(CharacterSheetVersion)
        .where(CharacterSheetVersion.player_character_id == player_character_id)
        .order_by(CharacterSheetVersion.version.desc())
    ).first()
    if latest is not None and latest.status in ("draft", "submitted"):
        raise HTTPException(
            status_code=409, detail="character already has an open draft"
        )

    version_number = 1 if latest is None else latest.version + 1
    derived = _derive_sheet_or_422(session, campaign_id, player_character_id, body)
    version = CharacterSheetVersion(
        campaign_id=campaign_id,
        player_character_id=player_character_id,
        version=version_number,
        sheet=body.model_dump(),
        derived=derived,
        created_by_email=email,
    )
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


@router.patch(
    "/campaigns/{campaign_id}/characters/{player_character_id}/sheets/"
    "{sheet_version_id}",
    response_model=CharacterSheetView,
)
def update_character_sheet_draft(
    campaign_id: str,
    player_character_id: str,
    sheet_version_id: str,
    body: CharacterSheetV1,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> CharacterSheetVersion:
    _require_character_owner(session, campaign_id, player_character_id, email)
    version = _get_sheet_version_or_404(
        session, campaign_id, player_character_id, sheet_version_id
    )
    if version.status != "draft":
        raise HTTPException(status_code=409, detail="only a draft can be edited")
    version.sheet = body.model_dump()
    version.derived = _derive_sheet_or_422(
        session, campaign_id, player_character_id, body
    )
    version.updated_at = datetime.now(timezone.utc)
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


@router.post(
    "/campaigns/{campaign_id}/characters/{player_character_id}/sheets/"
    "{sheet_version_id}/submit",
    response_model=CharacterSheetView,
)
def submit_character_sheet_draft(
    campaign_id: str,
    player_character_id: str,
    sheet_version_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> CharacterSheetVersion:
    _require_character_owner(session, campaign_id, player_character_id, email)
    version = _get_sheet_version_or_404(
        session, campaign_id, player_character_id, sheet_version_id
    )
    if version.status != "draft":
        raise HTTPException(status_code=409, detail="only a draft can be submitted")
    now = datetime.now(timezone.utc)
    version.status = "submitted"
    version.submitted_at = now
    version.updated_at = now
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


def _decide_character_sheet(
    *,
    session: Session,
    campaign_id: str,
    player_character_id: str,
    sheet_version_id: str,
    email: str,
    status: Literal["approved", "returned"],
    comment: str | None,
) -> CharacterSheetVersion:
    _require_dm(session, campaign_id, email)
    character = _get_character_in_campaign_or_404(
        session, campaign_id, player_character_id
    )
    version = _get_sheet_version_or_404(
        session, campaign_id, player_character_id, sheet_version_id
    )
    if version.status != "submitted":
        raise HTTPException(
            status_code=409, detail="only a submitted sheet can be decided"
        )
    if status == "returned" and comment is None:
        raise HTTPException(status_code=422, detail="a return comment is required")

    now = datetime.now(timezone.utc)
    version.status = status
    version.decision_comment = comment
    version.decided_by_email = email
    version.decided_at = now
    version.updated_at = now
    if status == "approved":
        character.class_name = version.sheet["class_name"]
        character.level = version.sheet["level"]
        character.sheet = version.sheet
        session.add(character)
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


@router.post(
    "/campaigns/{campaign_id}/characters/{player_character_id}/sheets/"
    "{sheet_version_id}/approve",
    response_model=CharacterSheetView,
)
def approve_character_sheet(
    campaign_id: str,
    player_character_id: str,
    sheet_version_id: str,
    body: SheetDecisionRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> CharacterSheetVersion:
    return _decide_character_sheet(
        session=session,
        campaign_id=campaign_id,
        player_character_id=player_character_id,
        sheet_version_id=sheet_version_id,
        email=email,
        status="approved",
        comment=body.comment,
    )


@router.post(
    "/campaigns/{campaign_id}/characters/{player_character_id}/sheets/"
    "{sheet_version_id}/return",
    response_model=CharacterSheetView,
)
def return_character_sheet(
    campaign_id: str,
    player_character_id: str,
    sheet_version_id: str,
    body: SheetDecisionRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> CharacterSheetVersion:
    return _decide_character_sheet(
        session=session,
        campaign_id=campaign_id,
        player_character_id=player_character_id,
        sheet_version_id=sheet_version_id,
        email=email,
        status="returned",
        comment=body.comment,
    )


# --- Campaign membership -----------------------------------------------


class MemberCreateRequest(BaseModel):
    email: str
    player_character_id: str | None = None


class BootstrapDmRequest(BaseModel):
    email: str


class MemberCharacterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player_character_id: str | None = None
    new: CharacterNameRequest | None = None

    @model_validator(mode="after")
    def exactly_one_character(self) -> MemberCharacterRequest:
        has_id = "player_character_id" in self.model_fields_set
        has_new = "new" in self.model_fields_set
        if has_id == has_new or (has_new and self.new is None):
            raise ValueError("provide exactly one of player_character_id or new")
        return self


class MemberView(BaseModel):
    id: str
    email: str
    role: MemberRole
    player_character_id: str | None
    character_name: str | None
    created_at: datetime


def _member_view(session: Session, member: CampaignMember) -> MemberView:
    user = session.get(AppUser, member.app_user_id)
    if user is None:
        raise HTTPException(status_code=500, detail="campaign member user missing")
    return MemberView(
        id=member.id,
        email=user.email,
        role=member.role,
        player_character_id=member.player_character_id,
        character_name=_member_character_name(session, member),
        created_at=member.created_at,
    )


def _member_character_name(session: Session, member: CampaignMember) -> str | None:
    if member.player_character_id is None:
        return None
    return _get_character_in_campaign_or_404(
        session, member.campaign_id, member.player_character_id
    ).character_name


@router.put(
    "/campaigns/{campaign_id}/members/{member_id}/character",
    response_model=MemberView,
)
def assign_member_character(
    campaign_id: str,
    member_id: str,
    body: MemberCharacterRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> MemberView:
    _require_dm(session, campaign_id, email)
    session.exec(
        select(Campaign)
        .where(Campaign.id == campaign_id)
        .with_for_update(key_share=True)
    ).one()
    # Self creation locks this row too; serialize against it before reading the link.
    # FOR NO KEY UPDATE still serializes concurrent PUTs but does not block
    # the FOR KEY SHARE a concurrent self-create insert takes on this row.
    member = session.exec(
        select(CampaignMember)
        .where(
            CampaignMember.id == member_id,
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.role == "player",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if member is None:
        raise HTTPException(status_code=404, detail="player membership not found")
    try:
        if body.new is not None:
            character = PlayerCharacter(
                campaign_id=campaign_id, character_name=body.new.name
            )
            session.add(character)
            session.flush()
            character_id = character.id
        else:
            character_id = body.player_character_id
            if character_id is not None:
                _get_character_in_campaign_or_404(session, campaign_id, character_id)
                assigned = session.exec(
                    select(CampaignMember).where(
                        CampaignMember.campaign_id == campaign_id,
                        CampaignMember.player_character_id == character_id,
                        CampaignMember.id != member.id,
                    )
                ).first()
                if assigned is not None:
                    raise HTTPException(
                        status_code=409, detail="character already assigned"
                    )
        member.player_character_id = character_id
        session.add(member)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="character already assigned"
        ) from exc
    session.refresh(member)
    return _member_view(session, member)


@router.post(
    "/campaigns/{campaign_id}/bootstrap-dm",
    response_model=MemberView,
)
def bootstrap_existing_campaign_dm(
    campaign_id: str,
    body: BootstrapDmRequest,
    _operator_email: str = Depends(get_grimoire_operator_email),
    session: Session = Depends(get_session),
) -> MemberView:
    """Explicitly attach the first DM to an upgraded existing campaign."""
    campaign = _get_campaign_or_404(session, campaign_id)
    existing_dm = session.exec(
        select(CampaignMember).where(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.role == "dm",
        )
    ).first()
    if existing_dm is not None:
        raise HTTPException(status_code=409, detail="campaign already has a DM")

    dm_email = body.email.strip().lower()
    if not dm_email or len(dm_email) > 320:
        raise HTTPException(status_code=422, detail="invalid DM email")
    user = _get_or_create_user(session, dm_email)
    existing_member = session.exec(
        select(CampaignMember).where(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.app_user_id == user.id,
        )
    ).first()
    if existing_member is not None:
        raise HTTPException(status_code=409, detail="campaign member already exists")

    campaign.owner_app_user_id = user.id
    session.add(campaign)
    member = CampaignMember(
        campaign_id=campaign_id,
        app_user_id=user.id,
        role="dm",
    )
    session.add(member)
    session.commit()
    session.refresh(member)
    return _member_view(session, member)


@router.post("/campaigns/{campaign_id}/members", response_model=MemberView)
def provision_player(
    campaign_id: str,
    body: MemberCreateRequest,
    _operator: str = Depends(get_grimoire_operator_email),
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> MemberView:
    """Operator repair path for legacy memberships."""
    _require_dm(session, campaign_id, email)
    invited_email = body.email.strip().lower()
    if not invited_email or len(invited_email) > 320:
        raise HTTPException(status_code=422, detail="invalid member email")
    if body.player_character_id is not None:
        _get_character_in_campaign_or_404(
            session, campaign_id, body.player_character_id
        )
        assigned = session.exec(
            select(CampaignMember).where(
                CampaignMember.campaign_id == campaign_id,
                CampaignMember.player_character_id == body.player_character_id,
            )
        ).first()
        if assigned is not None:
            raise HTTPException(status_code=409, detail="character already assigned")

    user = _get_or_create_user(session, invited_email)
    existing = session.exec(
        select(CampaignMember).where(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.app_user_id == user.id,
        )
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="campaign member already exists")

    member = CampaignMember(
        campaign_id=campaign_id,
        app_user_id=user.id,
        role="player",
        player_character_id=body.player_character_id,
    )
    session.add(member)
    session.commit()
    session.refresh(member)
    return _member_view(session, member)


@router.get("/campaigns/{campaign_id}/members", response_model=list[MemberView])
def list_members(
    campaign_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[MemberView]:
    _require_dm(session, campaign_id, email)
    members = session.exec(
        select(CampaignMember)
        .where(CampaignMember.campaign_id == campaign_id)
        .order_by(CampaignMember.created_at)
    ).all()
    return [_member_view(session, member) for member in members]


@router.delete(
    "/campaigns/{campaign_id}/members/{member_id}",
    status_code=204,
)
def revoke_player(
    campaign_id: str,
    member_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> None:
    _require_owner(session, campaign_id, email)
    session.exec(
        select(Campaign)
        .where(Campaign.id == campaign_id)
        .with_for_update(key_share=True)
    ).one()
    member = session.get(CampaignMember, member_id)
    if member is None or member.campaign_id != campaign_id or member.role != "player":
        raise HTTPException(status_code=404, detail="player membership not found")
    invitation = session.exec(
        select(CampaignInvitation)
        .where(
            CampaignInvitation.campaign_id == campaign_id,
            CampaignInvitation.invitee_id == member.app_user_id,
        )
        .with_for_update()
    ).first()
    if invitation is not None:
        invitation.status = "revoked"
        session.add(invitation)
    links = session.exec(
        select(CampaignJoinLink)
        .where(
            CampaignJoinLink.campaign_id == campaign_id,
            CampaignJoinLink.recipient_id == member.app_user_id,
            CampaignJoinLink.status.in_(["pending", "accepted"]),
        )
        .with_for_update()
    ).all()
    for link in links:
        link.status = "revoked"
    session.delete(member)
    session.commit()


# --- Knowledge grants ----------------------------------------------------


class GrantCreateRequest(BaseModel):
    entity_id: str
    player_character_id: str
    grant_scope: GrantScope
    revealed_details: dict | None = None
    granted_in_session: str | None = None


class GrantUpdateRequest(BaseModel):
    grant_scope: GrantScope | None = None
    revealed_details: dict | None = None


class BulkGrantItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str
    player_character_id: str
    grant_scope: GrantScope
    revealed_details: dict | None = None


class BulkGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grants: list[BulkGrantItem] = Field(min_length=1, max_length=50)
    granted_in_session: str | None = None

    @model_validator(mode="after")
    def shared_axis_and_unique_pairs(self):
        pairs = {
            (item.entity_id.casefold(), item.player_character_id.casefold())
            for item in self.grants
        }
        if len(pairs) != len(self.grants):
            raise ValueError("duplicate entity and character pair")
        if (
            len({item.entity_id.casefold() for item in self.grants}) != 1
            and len({item.player_character_id.casefold() for item in self.grants}) != 1
        ):
            raise ValueError("grants must share an entity or a player character")
        return self


class GrantView(BaseModel):
    id: str
    campaign_id: str
    entity_id: str
    player_character_id: str
    grant_scope: GrantScope
    revealed_details: dict | None
    granted_in_session: str | None
    created_at: datetime


def _validate_reveal_id(value: str, detail: str) -> None:
    """Accept dashed UUIDs in either case, preserving SQLite fixture spelling."""
    try:
        if value.casefold() != str(UUID(value)):
            raise ValueError("identifier must use dashed UUID spelling")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=detail) from exc


@router.post("/campaigns/{campaign_id}/grants/preview")
def preview_grants(
    campaign_id: str,
    body: BulkGrantRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[dict]:
    _require_dm(session, campaign_id, email)
    previews = []
    for item in body.grants:
        _get_character_in_campaign_or_404(
            session, campaign_id, item.player_character_id
        )
        entity = session.get(Entity, item.entity_id)
        if entity is None or not entity_belongs_to_campaign(
            session, campaign_id, entity
        ):
            raise HTTPException(status_code=404, detail="entity not found")
        detail_model = ENTITY_DETAIL_MODELS.get(entity.entity_type)
        detail = session.get(detail_model, entity.id) if detail_model else None
        grant = KnowledgeGrant(campaign_id=campaign_id, **item.model_dump())
        previews.append(
            {
                "player_character_id": item.player_character_id,
                "projection": jsonable_encoder(
                    project_entity(
                        entity,
                        detail,
                        grant,
                        item.player_character_id,
                        context="relationship",
                    )
                ),
            }
        )
    return previews


def _get_character_in_campaign_or_404(
    session: Session, campaign_id: str, player_character_id: str
) -> PlayerCharacter:
    character = session.get(PlayerCharacter, player_character_id)
    if character is None or character.campaign_id != campaign_id:
        raise HTTPException(
            status_code=404,
            detail="player character not found in this campaign",
        )
    return character


def _current_reveal_session(session: Session, campaign_id: str) -> GameSession | None:
    if not play_enabled():
        return None
    return session.exec(
        select(GameSession).where(
            GameSession.campaign_id == campaign_id,
            GameSession.status.in_(("active", "paused")),
        )
    ).first()


def _reveal_identity(entity: Entity, grant: KnowledgeGrant) -> dict[str, Any]:
    return {
        "entity_id": entity.id,
        "name": entity.name,
        "entity_type": entity.entity_type,
        "grant_scope": grant.grant_scope,
    }


def _reveal_body(session: Session, grant: KnowledgeGrant) -> dict[str, Any]:
    """Snapshot only the persisted grant's grantee projection, JSON-safe."""
    entity = session.get(Entity, grant.entity_id)
    body = _reveal_identity(entity, grant)
    if grant.grant_scope != "name_only":
        detail_model = ENTITY_DETAIL_MODELS.get(entity.entity_type)
        detail = session.get(detail_model, entity.id) if detail_model else None
        body["entity"] = project_entity(
            entity, detail, grant, grant.player_character_id, context="lookup"
        )
    return jsonable_encoder(body)


def _append_reveal(
    session: Session,
    game_session: GameSession,
    member: CampaignMember,
    pc_id: str,
    body: dict[str, Any],
) -> None:
    try:
        append_event(
            session,
            game_session=game_session,
            kind="reveal",
            audience=Audience("pcs", {pc_id}),
            author_member_id=member.id,
            body=body,
        )
    except SessionEndedError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise


@router.post("/campaigns/{campaign_id}/grants", response_model=GrantView)
def create_grant(
    campaign_id: str,
    body: GrantCreateRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> KnowledgeGrant:
    member = _require_dm(session, campaign_id, email)
    character = _get_character_in_campaign_or_404(
        session, campaign_id, body.player_character_id
    )
    # Validate the entity exists before insert: knowledge_grant.entity_id is a
    # FK, so on Postgres a missing entity raises IntegrityError and surfaces as
    # an unhandled 500. (SQLite fixtures do not enforce FKs, so this guard is
    # what makes the 404 behavior consistent across both backends.)
    entity = session.get(Entity, body.entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="entity not found")
    if not entity_belongs_to_campaign(session, campaign_id, entity):
        raise HTTPException(status_code=404, detail="entity not found")
    if body.granted_in_session is not None:
        game_session = session.get(GameSession, body.granted_in_session)
        if game_session is None or game_session.campaign_id != campaign_id:
            raise HTTPException(status_code=404, detail="session not found")

    existing = session.exec(
        select(KnowledgeGrant).where(
            KnowledgeGrant.entity_id == body.entity_id,
            KnowledgeGrant.player_character_id == body.player_character_id,
        )
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="grant already exists for this entity and character",
        )

    current = _current_reveal_session(session, campaign_id)
    grant = KnowledgeGrant(
        campaign_id=campaign_id,
        entity_id=body.entity_id,
        player_character_id=(
            body.player_character_id if current is None else character.id
        ),
        grant_scope=body.grant_scope,
        revealed_details=body.revealed_details,
        granted_in_session=(
            body.granted_in_session
            if body.granted_in_session is not None or current is None
            else current.id
        ),
    )
    session.add(grant)
    if current is not None:
        session.flush()
        _append_reveal(
            session,
            current,
            member,
            grant.player_character_id,
            _reveal_body(session, grant),
        )
    session.commit()
    session.refresh(grant)
    return grant


@router.post(
    "/campaigns/{campaign_id}/grants/bulk",
    response_model=list[GrantView],
    dependencies=[Depends(require_play_enabled)],
)
def create_bulk_grants(
    campaign_id: str,
    body: BulkGrantRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[KnowledgeGrant]:
    """Validate the entire batch, then commit grants and per-PC reveals once."""
    member = _require_dm(session, campaign_id, email)
    if body.granted_in_session is not None:
        _validate_reveal_id(body.granted_in_session, "session not found")
        explicit = session.get(GameSession, body.granted_in_session)
        if explicit is None or explicit.campaign_id != campaign_id:
            raise HTTPException(status_code=404, detail="session not found")
    validated: list[tuple[BulkGrantItem, str, str]] = []
    for item in body.grants:
        _validate_reveal_id(
            item.player_character_id, "player character not found in this campaign"
        )
        _validate_reveal_id(item.entity_id, "entity not found")
        character = _get_character_in_campaign_or_404(
            session, campaign_id, item.player_character_id
        )
        entity = session.get(Entity, item.entity_id)
        if entity is None or not entity_belongs_to_campaign(
            session, campaign_id, entity
        ):
            raise HTTPException(status_code=404, detail="entity not found")
        existing = session.exec(
            select(KnowledgeGrant.id).where(
                KnowledgeGrant.entity_id == entity.id,
                KnowledgeGrant.player_character_id == character.id,
            )
        ).first()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail="grant already exists for this entity and character",
            )
        validated.append((item, entity.id, character.id))

    current = _current_reveal_session(session, campaign_id)
    grants = [
        KnowledgeGrant(
            campaign_id=campaign_id,
            entity_id=entity_id,
            player_character_id=pc_id,
            grant_scope=item.grant_scope,
            revealed_details=item.revealed_details,
            granted_in_session=(
                body.granted_in_session
                if body.granted_in_session is not None or current is None
                else current.id
            ),
        )
        for item, entity_id, pc_id in validated
    ]
    session.add_all(grants)
    session.flush()
    if current is not None:
        reveals: dict[str, list[dict[str, Any]]] = {}
        for grant in grants:
            reveals.setdefault(grant.player_character_id, []).append(
                _reveal_body(session, grant)
            )
        for pc_id, bodies in reveals.items():
            _append_reveal(session, current, member, pc_id, {"reveals": bodies})
    session.commit()
    for grant in grants:
        session.refresh(grant)
    return grants


@router.get("/campaigns/{campaign_id}/grants", response_model=list[GrantView])
def list_grants(
    campaign_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[KnowledgeGrant]:
    _require_dm(session, campaign_id, email)
    return session.exec(
        select(KnowledgeGrant)
        .where(KnowledgeGrant.campaign_id == campaign_id)
        .order_by(KnowledgeGrant.created_at)
    ).all()


def _retract_grant_history(session: Session, grant: KnowledgeGrant) -> None:
    previous = session.exec(
        select(SessionEvent).where(
            SessionEvent.campaign_id == grant.campaign_id,
            SessionEvent.kind == "reveal",
            SessionEvent.retracted_at.is_(None),
        )
    ).all()
    changed = []
    for event in previous:
        if (
            "reveals" in event.body
            and grant.player_character_id in event.audience_pc_ids
        ):
            if any(
                item["entity_id"] == grant.entity_id
                for item in reveal_items(event.body)
            ):
                event.body = {
                    **event.body,
                    "retracted_entity_ids": [
                        *event.body.get("retracted_entity_ids", []),
                        grant.entity_id,
                    ],
                }
                if not reveal_items(event.body):
                    event.retracted_at = datetime.now(timezone.utc)
                sync_event_embeddings(session, event)
                changed.append(event)
            continue
        if (
            event.body.get("entity_id") == grant.entity_id
            and grant.player_character_id in event.audience_pc_ids
        ):
            event.retracted_at = datetime.now(timezone.utc)
            sync_event_embeddings(session, event)
            changed.append(event)
    session.add_all(changed)


@router.patch(
    "/campaigns/{campaign_id}/grants/{grant_id}",
    response_model=GrantView,
)
def update_grant(
    campaign_id: str,
    grant_id: str,
    body: GrantUpdateRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> KnowledgeGrant:
    member = _require_dm(session, campaign_id, email)
    grant = session.get(KnowledgeGrant, grant_id)
    if grant is None or grant.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="grant not found")

    previous_scope = grant.grant_scope
    previous_details = grant.revealed_details
    if body.grant_scope is not None:
        grant.grant_scope = body.grant_scope
    if body.revealed_details is not None:
        grant.revealed_details = body.revealed_details

    session.add(grant)
    if grant.grant_scope != previous_scope or (
        grant.grant_scope == "partial" and grant.revealed_details != previous_details
    ):
        if play_enabled():
            _retract_grant_history(session, grant)
        current = _current_reveal_session(session, campaign_id)
        if current is not None:
            session.flush()
            _append_reveal(
                session,
                current,
                member,
                grant.player_character_id,
                _reveal_body(session, grant),
            )
    session.commit()
    session.refresh(grant)
    return grant


@router.delete("/campaigns/{campaign_id}/grants/{grant_id}", status_code=204)
def delete_grant(
    campaign_id: str,
    grant_id: str,
    silent: bool = Query(default=False),
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> None:
    """Revoke a grant (the grant editor's "none" scope). Idempotent-ish: a
    missing grant is a 404, matching update_grant's not-found semantics. Removing
    a grant on a non-global entity returns it to invisible for that character;
    on a global entity it drops the character back to the default full view."""
    member = _require_dm(session, campaign_id, email)
    grant = session.get(KnowledgeGrant, grant_id)
    if grant is None or grant.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="grant not found")
    if play_enabled():
        _retract_grant_history(session, grant)
    current = _current_reveal_session(session, campaign_id)
    if current is not None:
        body = {"retracted": True, "silent": silent}
        if not silent:
            body.update(_reveal_identity(session.get(Entity, grant.entity_id), grant))
        _append_reveal(session, current, member, grant.player_character_id, body)
    session.delete(grant)
    session.commit()


# --- Entities (grant-filtered read paths) --------------------------------

# All read paths below build on visible_entities_query()/project_entity()
# from visibility.py rather than reimplementing the grant predicate.


def _aggregate_dm_rows(
    rows: list[tuple[Entity, KnowledgeGrant | None]],
    *,
    detail=None,
    context: str = "lookup",
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Collapses the DM view's one-row-per-grant rows to one item per entity.

    visible_entities_query() for the DM viewer joins on campaign only (not a
    single player_character_id), so an entity with N grants comes back as N
    row tuples sharing the same Entity. This folds those into a single
    projected dict per entity with a "grants" list, preserving first-seen
    order.
    """
    aggregated: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for entity, grant in rows:
        projected = project_entity(entity, detail, grant, "dm", context=context)
        grant_dict = projected.pop("grant")
        if grant_dict is not None:
            grant_dict = {"id": grant.id, **grant_dict}
        existing = aggregated.get(entity.id)
        if existing is None:
            projected["grants"] = [grant_dict] if grant_dict else []
            aggregated[entity.id] = projected
            order.append(entity.id)
        elif grant_dict:
            existing["grants"].append(grant_dict)
    return aggregated, order


def _project_neighbor(
    session: Session, campaign_id: str, viewer: Viewer, neighbor_id: str
) -> dict[str, Any] | None:
    """Projects a relationship neighbor in "relationship" context.

    Returns None when the neighbor is not visible to the viewer at all
    (ungranted and not global), so the caller can drop the edge entirely.
    A name_only grant still yields a recognition stub (not None) because
    context="relationship" tells project_entity() this is neighbor listing,
    not direct lookup. Never loads typed detail: neighbor listings stay
    spine-level like the list endpoint.
    """
    rows = session.exec(
        visible_entities_query(campaign_id, viewer).where(Entity.id == neighbor_id)
    ).all()
    if not rows:
        return None
    if viewer == "dm":
        aggregated, order = _aggregate_dm_rows(rows, context="relationship")
        return aggregated[order[0]]
    entity, grant = rows[0]
    return project_entity(entity, None, grant, viewer, context="relationship")


def _parse_offset(cursor: str | None) -> int:
    """Decode the opaque entity-list cursor (a stringified offset). A missing or
    malformed cursor starts from the top rather than erroring, so a stale cursor
    degrades to page one instead of a 422."""
    if cursor is None:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError:
        return 0


def _authorize_reveal_filter(
    session: Session,
    campaign_id: str,
    member: CampaignMember,
    not_granted_to: str | None,
) -> None:
    if not_granted_to is None:
        return
    require_play_enabled()
    if member.role != "dm":
        raise HTTPException(status_code=403, detail="campaign DM role required")
    _validate_reveal_id(not_granted_to, "player character not found in this campaign")
    _get_character_in_campaign_or_404(session, campaign_id, not_granted_to)


@router.get("/campaigns/{campaign_id}/entities")
def list_entities(
    campaign_id: str,
    entity_type: EntityType | None = Query(default=None, alias="type"),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    not_granted_to: str | None = Query(default=None),
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Grant-filtered, paginated entity list, spine-level only (no typed detail).

    Returns ``{items, total, next_cursor}``. Item dict shape is scope-dependent
    (full spine, partial identity + revealed_details, or a full spine + grants
    annotation for the DM), so items are plain dicts rather than a fixed
    response_model, matching the heterogeneous-payload pattern used by
    knowledge/router.py.

    DM-only ``not_granted_to`` means "hide entities that PC already knows",
    including global entities and grants of any scope, behind the play flag.

    The projection (DM grant-aggregation, player grant-filtering, name_only
    suppression) has to run in Python before the page is known, so the full
    visible set is materialised and then sliced by ``cursor``/``limit``. At v1
    corpus scale (hundreds of entities) this stays cheap; keyset pagination
    would have to reconcile the DM view's one-row-per-grant fan-out and is not
    worth it yet.
    """
    member = _get_member_or_404(session, campaign_id, email)
    _authorize_reveal_filter(session, campaign_id, member, not_granted_to)
    viewer = _viewer_for_member(session, campaign_id, member)

    query = visible_entities_query(
        campaign_id, viewer, not_granted_to=not_granted_to
    ).order_by(Entity.name)
    if entity_type is not None:
        query = query.where(Entity.entity_type == entity_type)
    if q:
        query = query.where(func.lower(Entity.name).contains(q.lower()))

    rows = session.exec(query).all()

    if viewer == "dm":
        aggregated, order = _aggregate_dm_rows(rows, context="lookup")
        items = [aggregated[entity_id] for entity_id in order]
    else:
        items = []
        for entity, grant in rows:
            projected = project_entity(entity, None, grant, viewer, context="lookup")
            if projected is not None:
                items.append(projected)

    total = len(items)
    offset = _parse_offset(cursor)
    page = items[offset : offset + limit]
    next_offset = offset + limit
    next_cursor = str(next_offset) if next_offset < total else None
    return {"items": page, "total": total, "next_cursor": next_cursor}


@router.get("/campaigns/{campaign_id}/entities/{entity_id}")
def get_entity(
    campaign_id: str,
    entity_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Single entity, scope-projected, with typed detail hydrated.

    Existence must not leak past the grant predicate: both a non-visible
    entity and a name_only grant return a plain 404 (project_entity()
    returns None for name_only in lookup context, so both cases collapse to
    the same check below).
    """
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)

    rows = session.exec(
        visible_entities_query(campaign_id, viewer).where(Entity.id == entity_id)
    ).all()
    if not rows:
        raise HTTPException(status_code=404, detail="entity not found")

    entity = rows[0][0]
    detail_model = ENTITY_DETAIL_MODELS.get(entity.entity_type)
    detail = session.get(detail_model, entity_id) if detail_model else None

    if viewer == "dm":
        aggregated, order = _aggregate_dm_rows(rows, detail=detail, context="lookup")
        return aggregated[order[0]]

    grant = rows[0][1]
    projected = project_entity(entity, detail, grant, viewer, context="lookup")
    if projected is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return projected


@router.get("/campaigns/{campaign_id}/entities/{entity_id}/relationships")
def list_entity_relationships(
    campaign_id: str,
    entity_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """1-hop relationship edges from/to entity_id, grant-filtered per neighbor.

    The center entity must itself be visible to the viewer in lookup context
    (same 404-or-not check as get_entity, existence must not leak). Each
    neighbor is then projected independently in "relationship" context so a
    name_only neighbor becomes a recognition stub instead of vanishing, while
    a wholly invisible neighbor (ungranted, non-global) drops its edge.
    """
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)

    center_rows = session.exec(
        visible_entities_query(campaign_id, viewer).where(Entity.id == entity_id)
    ).all()
    if not center_rows:
        raise HTTPException(status_code=404, detail="entity not found")
    if viewer != "dm":
        center_entity, center_grant = center_rows[0]
        center_projected = project_entity(
            center_entity, None, center_grant, viewer, context="lookup"
        )
        if center_projected is None:
            raise HTTPException(status_code=404, detail="entity not found")

    outgoing = session.exec(
        select(Relationship).where(Relationship.from_entity_id == entity_id)
    ).all()
    incoming = session.exec(
        select(Relationship).where(Relationship.to_entity_id == entity_id)
    ).all()
    edges = [(rel, "out", rel.to_entity_id) for rel in outgoing] + [
        (rel, "in", rel.from_entity_id) for rel in incoming
    ]

    items: list[dict[str, Any]] = []
    for rel, direction, neighbor_id in edges:
        neighbor = _project_neighbor(session, campaign_id, viewer, neighbor_id)
        if neighbor is None:
            continue
        items.append(
            {
                "rel_type": rel.rel_type,
                "direction": direction,
                "properties": rel.properties,
                "entity": neighbor,
            }
        )
    return items


@router.get("/campaigns/{campaign_id}/entities/{entity_id}/mentions")
def list_entity_mentions(
    campaign_id: str,
    entity_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """Chunks that mention this entity (the "Sources" list on entity detail).

    Gated behind the same visibility check as get_entity: the viewer must be
    able to see the entity in lookup context, or this 404s (existence must not
    leak, and a name_only viewer gets nothing, consistent with the entity
    detail 404). Chunks themselves are corpus-global in v1, so once the gate
    passes every mention is returned.
    """
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)

    rows = session.exec(
        visible_entities_query(campaign_id, viewer).where(Entity.id == entity_id)
    ).all()
    if not rows:
        raise HTTPException(status_code=404, detail="entity not found")
    if viewer != "dm":
        entity, grant = rows[0]
        if project_entity(entity, None, grant, viewer, context="lookup") is None:
            raise HTTPException(status_code=404, detail="entity not found")

    return library.list_entity_mentions(session, entity_id)


# --- Library / corpus (book + chunk reads, corpus-global) ------------

# Books and chunks are NOT campaign-scoped: the corpus is global in v1 (matching
# search._resolve_chunk_hit). Only a chunk's on-page entity chips take a
# campaign + viewpoint, since those are grant-projected; everything else here is
# plain corpus data. All aggregation lives in library.py so these handlers stay
# thin.


class BookRenameRequest(BaseModel):
    display_name: str


@router.get("/books")
def list_books(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Per-book coverage rows for the Library (counts, extraction progress,
    entity yield, timestamps). See library.list_books."""
    return library.list_books(session)


@router.patch("/books/{book_id}")
def rename_book(
    book_id: str,
    body: BookRenameRequest,
    _operator_email: str = Depends(get_grimoire_operator_email),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Rename a book (set its display_name) from the Library UI."""
    display_name = body.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=422, detail="display_name must not be empty")
    book = library.rename_book(session, book_id, display_name)
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    return {"book_id": book.id, "display_name": book.display_name}


@router.get("/books/{book_id}/sections")
def list_book_sections(
    book_id: str, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    """Ordered section tree for one book (reading order, chunk counts)."""
    return library.list_sections(session, book_id)


@router.get("/books/{book_id}/chunks")
def list_book_chunks(
    book_id: str,
    section: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(
        default=library.DEFAULT_CHUNK_PAGE, ge=1, le=library.MAX_CHUNK_PAGE
    ),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Seq-ordered page of a book's chunks (optionally one section)."""
    return library.list_chunks(
        session, book_id, section=section, cursor=cursor, limit=limit
    )


@router.get("/books/{book_id}/read")
def read_book(
    book_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(
        default=library.DEFAULT_READ_PAGE, ge=1, le=library.MAX_READ_PAGE
    ),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Seq-ordered page of FULL chunks for the continuous reader. Corpus-
    global like the chunk body itself; image chunks carry the bucket-relative
    object key the frontend server signs into an imgproxy URL."""
    return library.read_page(session, book_id, cursor=cursor, limit=limit)


@router.get("/chunks/{chunk_id}")
def get_chunk(
    chunk_id: str,
    campaign: str = Query(),
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """One chunk with full content, image URL, seq neighbours, and on-page
    entity chips projected for the (campaign, viewpoint). The campaign/viewpoint
    only shape the entity chips; the chunk body is corpus-global."""
    member = _get_member_or_404(session, campaign, email)
    viewer = _viewer_for_member(session, campaign, member)
    chunk = library.get_chunk(session, campaign, viewer, chunk_id)
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

    404s for a missing chunk or a text chunk (no image_ref). Reads the object
    from the shared SeaweedFS S3 endpoint the loader already uses (the API pod
    carries the same SEAWEEDFS_S3_ENDPOINT/S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY
    env under the always-set ``stars`` block, so no chart change is needed).
    This is a sync ``def`` handler, so FastAPI runs the blocking boto3 read in
    its threadpool, off the event loop. imgproxy resizing is a later
    optimization (recorded as a follow-up, not built here).
    """
    from grimoire.ingest import build_s3_client

    chunk = session.get(KnowledgeChunk, chunk_id)
    if chunk is None or not chunk.image_ref:
        raise HTTPException(status_code=404, detail="chunk image not found")
    parsed = _parse_s3_uri(chunk.image_ref)
    if parsed is None:
        raise HTTPException(status_code=404, detail="chunk image not found")
    bucket, key = parsed

    suffix = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    content_type = _IMAGE_CONTENT_TYPES.get(suffix, "application/octet-stream")

    client = build_s3_client()
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - any S3 miss/error becomes a 404
        # Routine for text chunks probed directly; keep the log quiet but keyed.
        logger.info("chunk image fetch failed for %s: %s", key, exc)
        raise HTTPException(status_code=404, detail="chunk image not found") from exc

    body = obj["Body"]

    def _stream():
        try:
            for part in body.iter_chunks(chunk_size=64 * 1024):
                yield part
        finally:
            body.close()

    return StreamingResponse(_stream(), media_type=content_type)


# --- Vector search ---------------------------------------------------

# The embedding client is injected through knowledge.api.get_embedding_client
# (the sanctioned cross-domain import boundary, see knowledge/api.py's
# docstring), rather than grimoire duplicating its own copy: knowledge
# already owns this DI seam and grimoire has no reason to diverge from it.
# Tests override it the same way knowledge's own tests do, via
# ``app.dependency_overrides[get_embedding_client]``.


@router.get("/campaigns/{campaign_id}/search")
async def search_campaign_route(
    campaign_id: str,
    q: str = Query(min_length=1),
    k: int = Query(default=10, ge=1, le=50),
    not_granted_to: str | None = Query(default=None),
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
    embed_client: EmbeddingClient = Depends(get_embedding_client),
) -> list[dict[str, Any]]:
    """kNN search over embedding, grant-filtered, mixed entity/chunk hits.

    All visibility filtering happens in search.search_campaign, which builds
    on the same visible_entities_query()/project_entity() helpers as the
    entity read paths above.

    DM-only ``not_granted_to`` means "hide entities that PC already knows",
    including global entities and any grant scope. Chunk hits are unaffected;
    fewer than k hits are acceptable. The filter requires the play flag.
    """
    member = _get_member_or_404(session, campaign_id, email)
    _authorize_reveal_filter(session, campaign_id, member, not_granted_to)
    viewer = _viewer_for_member(session, campaign_id, member)
    return await search_campaign(
        session,
        embed_client,
        campaign_id,
        viewer,
        q,
        k=k,
        not_granted_to=not_granted_to,
    )


@router.get(
    "/campaigns/{campaign_id}/knowledge/search",
    dependencies=[Depends(require_play_enabled)],
)
async def search_knowledge_route(
    campaign_id: str,
    q: str = Query(min_length=1, max_length=200),
    k: int = Query(default=10, ge=1, le=50),
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
    embed_client: EmbeddingClient = Depends(get_embedding_client),
) -> list[dict[str, Any]]:
    """Visible entities, notes, journal events and corpus chunks for a member."""
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    return await search_knowledge(
        session, embed_client, campaign_id, viewer, member, q, k
    )


# --- Game sessions ---------------------------------------------------


class GameSessionUpdateRequest(BaseModel):
    status: SessionStatus


class GameSessionView(BaseModel):
    id: str
    campaign_id: str
    status: SessionStatus
    started_at: datetime
    ended_at: datetime | None


@router.get(
    "/campaigns/{campaign_id}/sessions",
    response_model=list[GameSessionView],
    dependencies=[Depends(require_play_enabled)],
)
def list_game_sessions(
    campaign_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[GameSession]:
    _get_member_or_404(session, campaign_id, email)
    return session.exec(
        select(GameSession)
        .where(GameSession.campaign_id == campaign_id)
        .order_by(GameSession.started_at.desc(), GameSession.id.desc())
    ).all()


@router.get(
    "/campaigns/{campaign_id}/sessions/current",
    response_model=GameSessionView,
    dependencies=[Depends(require_play_enabled)],
)
def current_game_session(
    campaign_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> GameSession:
    _get_member_or_404(session, campaign_id, email)
    row = session.exec(
        select(GameSession).where(
            GameSession.campaign_id == campaign_id,
            GameSession.status.in_(("active", "paused")),
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="game session not found")
    return row


@router.post(
    "/campaigns/{campaign_id}/sessions",
    response_model=GameSessionView,
)
def create_game_session(
    campaign_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> GameSession:
    _require_dm(session, campaign_id, email)

    active = session.exec(
        select(GameSession).where(
            GameSession.campaign_id == campaign_id,
            or_(GameSession.status != "ended", GameSession.status.is_(None)),
        )
    ).first()
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail="an active or paused session already exists for this campaign",
        )

    game_session = GameSession(campaign_id=campaign_id)
    session.add(game_session)
    session.commit()
    session.refresh(game_session)
    return game_session


@router.patch(
    "/campaigns/{campaign_id}/sessions/{session_id}",
    response_model=GameSessionView,
)
def update_game_session(
    campaign_id: str,
    session_id: str,
    body: GameSessionUpdateRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> GameSession:
    _require_dm(session, campaign_id, email)
    game_session = session.get(GameSession, session_id)
    if game_session is None or game_session.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="game session not found")

    game_session.status = body.status
    if body.status == "ended" and game_session.ended_at is None:
        game_session.ended_at = datetime.now(timezone.utc)

    session.add(game_session)
    session.commit()
    session.refresh(game_session)
    return game_session


# --- Audience-scoped session event log --------------------------------


class SessionEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EventKind
    audience: AudienceKind
    audience_pc_ids: list[str] = Field(default_factory=list)
    body: dict[str, Any]
    request_id: UUID | None = None

    @field_validator("audience_pc_ids")
    @classmethod
    def valid_pc_uuids(cls, values: list[str]) -> list[str]:
        for value in values:
            UUID(value)
        return values


class SessionEventView(BaseModel):
    id: str
    campaign_id: str
    session_id: str
    seq: int
    kind: EventKind
    audience: AudienceKind
    audience_pc_ids: list[str]
    author_member_id: str | None
    body: dict[str, Any] | None
    created_at: datetime
    retracted_at: datetime | None

    @field_validator("created_at", "retracted_at")
    @classmethod
    def timestamps_as_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _session_in_campaign(
    session: Session, campaign_id: str, session_id: str
) -> GameSession:
    row = session.get(GameSession, session_id)
    if row is None or row.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="game session not found")
    return row


def _event_view(row: SessionEvent, member: CampaignMember) -> SessionEventView:
    """Project retractions and avoid exposing other members' administrative ids."""
    dm = member.role == "dm"
    body = row.body if dm or row.retracted_at is None else None
    if not dm and body and ("reveals" in body or row.kind == "reveal"):
        items = [
            {
                key: value
                for key, value in item.items()
                if key not in ("text", "entity", "projection")
            }
            if item.get("grant_scope") == "name_only"
            else item
            for item in reveal_items(body)
        ]
        body = (
            {"reveals": items} if "reveals" in body else (items[0] if items else body)
        )
    return SessionEventView(
        id=row.id,
        campaign_id=row.campaign_id,
        session_id=row.session_id,
        seq=row.seq,
        kind=row.kind,
        audience=row.audience,
        audience_pc_ids=(
            row.audience_pc_ids
            if dm
            else [pc for pc in row.audience_pc_ids if pc == member.player_character_id]
        ),
        author_member_id=(
            row.author_member_id if dm or row.author_member_id == member.id else None
        ),
        body=body,
        created_at=row.created_at,
        retracted_at=row.retracted_at,
    )


class RollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formula: str
    label: str | None = None
    visibility: Literal["table", "dm", "self"] | None = None

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if len(value) > 200:
            raise ValueError("label must be at most 200 characters")
        return value


def _require_roll_scope(
    campaign_id: str,
    session_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> None:
    """Authorize membership and session scope before roll body validation.

    Runs as a route dependency so nonmembers receive 404 before FastAPI
    validates the RollRequest body schema.
    """
    _get_member_or_404(session, campaign_id, email)
    _session_in_campaign(session, campaign_id, session_id)


def _require_journal_campaign_scope(
    campaign_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> None:
    _validate_reveal_id(campaign_id, "campaign not found")
    _get_member_or_404(session, campaign_id, email)


def _require_journal_session_scope(
    campaign_id: str,
    session_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> None:
    _require_journal_campaign_scope(campaign_id, email, session)
    _validate_reveal_id(session_id, "game session not found")
    _session_in_campaign(session, campaign_id, session_id)


def _journal_context(session, campaign_id, viewer, member, events, view):
    rows = visible_rows(viewer, member, events, view)
    candidates = narration_entity_ids(rows)
    entities = {}
    if candidates:
        for entity, grant in session.exec(
            visible_entities_query(campaign_id, viewer).where(
                Entity.id.in_(candidates | {value.upper() for value in candidates})
            )
        ).all():
            projection = project_entity(
                entity, None, grant, viewer, context="relationship"
            )
            if projection is not None:
                entities[str(UUID(entity.id))] = {
                    key: projection[key] for key in ("id", "name", "entity_type")
                }
    # Only pairs referenced by reveal rows are needed for silent revocation.
    reveal_ids = set()
    for row in rows:
        if row.kind != "reveal":
            continue
        bodies = row.body.get("reveals", [row.body])
        if isinstance(bodies, list):
            for body in bodies:
                if isinstance(body, dict) and isinstance(body.get("entity_id"), str):
                    try:
                        reveal_ids.add(str(UUID(body["entity_id"])))
                    except ValueError:
                        continue
    grants = set()
    if reveal_ids:
        query = select(
            KnowledgeGrant.player_character_id, KnowledgeGrant.entity_id
        ).where(
            KnowledgeGrant.campaign_id == campaign_id,
            KnowledgeGrant.entity_id.in_(
                reveal_ids | {value.upper() for value in reveal_ids}
            ),
        )
        if viewer != "dm":
            query = query.where(KnowledgeGrant.player_character_id == viewer)
        grants = set(session.exec(query).all())
    return grants, entities


# Bound on audience-visible events folded into one session journal. A single
# long-running session must not make a journal read materialize unbounded
# rows, so reads stop at the earliest budgeted events per session and say so
# via Journal.truncated instead of silently folding a truncated stream.
JOURNAL_EVENTS_PER_SESSION = 500


def _journal_events(session, campaign_id, session_ids, viewer, member, view):
    """Load at most JOURNAL_EVENTS_PER_SESSION projected events per session.

    The budget counts only rows the journal can project: retracted rows and,
    for the party view, non-table rows are excluded before ranking so they
    never consume it.

    Returns (events, truncated_by_session): events holds the earliest
    budgeted rows per session in (session_id, seq, id) order, and the map
    flags the sessions whose visible stream exceeded the budget. The probe
    row per session is read but never folded, so overflow is explicit.
    """
    if not session_ids:
        return [], {}
    projected = [
        SessionEvent.campaign_id == campaign_id,
        SessionEvent.session_id.in_(session_ids),
        SessionEvent.retracted_at.is_(None),
        audience_predicate(SessionEvent, viewer, member),
    ]
    if view == "party":
        projected.append(SessionEvent.audience == "table")
    ranked = (
        select(
            SessionEvent.id,
            func.row_number()
            .over(
                partition_by=SessionEvent.session_id,
                order_by=[SessionEvent.seq, SessionEvent.id],
            )
            .label("rn"),
        )
        .where(*projected)
        .subquery("journal_ranked")
    )
    # Belt and braces: the per-session rn filter already caps rows at
    # len(session_ids) * (budget + 1), so this outer LIMIT never engages.
    # It keeps an explicit LIMIT in the journal event SQL even if the
    # window predicate is ever altered.
    outer_limit = len(session_ids) * (JOURNAL_EVENTS_PER_SESSION + 1) + 1
    rows = session.exec(
        select(SessionEvent)
        .where(
            SessionEvent.id.in_(
                select(ranked.c.id).where(ranked.c.rn <= JOURNAL_EVENTS_PER_SESSION + 1)
            )
        )
        .order_by(SessionEvent.session_id, SessionEvent.seq, SessionEvent.id)
        .limit(outer_limit)
    ).all()
    events: list[SessionEvent] = []
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.session_id] = counts.get(row.session_id, 0) + 1
        if counts[row.session_id] <= JOURNAL_EVENTS_PER_SESSION:
            events.append(row)
    truncated = {
        session_id: counts.get(session_id, 0) > JOURNAL_EVENTS_PER_SESSION
        for session_id in session_ids
    }
    return events, truncated


@router.get(
    "/campaigns/{campaign_id}/sessions/{session_id}/journal",
    response_model=Journal,
    dependencies=[
        Depends(require_play_enabled),
        Depends(_require_journal_session_scope),
    ],
)
def get_session_journal(
    campaign_id: str,
    session_id: str,
    view: Literal["mine", "party"] = "mine",
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> Journal:
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    events, truncated = _journal_events(
        session, campaign_id, [session_id], viewer, member, view
    )
    grants, entities = _journal_context(
        session, campaign_id, viewer, member, events, view
    )
    result = journal(
        viewer,
        member,
        events,
        current_grants=grants,
        visible_entities=entities,
        view=view,
    )
    result.truncated = truncated.get(session_id, False)
    return result


class SessionJournalView(BaseModel):
    session_id: str
    started_at: datetime
    journal: Journal


class CampaignJournalView(BaseModel):
    sessions: list[SessionJournalView]
    next_cursor: str | None


def _journal_cursor(row: GameSession) -> str:
    timestamp = row.started_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    value = json.dumps([timestamp.isoformat(), row.id]).encode()
    return base64.urlsafe_b64encode(value).decode()


def _read_journal_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        timestamp, session_id = json.loads(
            base64.b64decode(cursor, altchars=b"-_", validate=True)
        )
        if not isinstance(timestamp, str) or not isinstance(session_id, str):
            raise TypeError("invalid journal cursor")
        timestamp = datetime.fromisoformat(timestamp)
        if timestamp.tzinfo is None or session_id.casefold() != str(UUID(session_id)):
            raise ValueError("invalid journal cursor")
        return timestamp, session_id
    except (ValueError, TypeError, binascii.Error, UnicodeError) as exc:
        raise HTTPException(422, detail="invalid journal cursor") from exc


@router.get(
    "/campaigns/{campaign_id}/journal",
    response_model=CampaignJournalView,
    dependencies=[
        Depends(require_play_enabled),
        Depends(_require_journal_campaign_scope),
    ],
)
def get_campaign_journal(
    campaign_id: str,
    view: Literal["mine", "party"] = "mine",
    limit: int = Query(default=10, ge=1, le=50),
    cursor: str | None = Query(default=None, max_length=256),
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> CampaignJournalView:
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    query = select(GameSession).where(GameSession.campaign_id == campaign_id)
    if cursor is not None:
        timestamp, session_id = _read_journal_cursor(cursor)
        query = query.where(
            or_(
                GameSession.started_at < timestamp,
                (GameSession.started_at == timestamp) & (GameSession.id < session_id),
            )
        )
    sessions = session.exec(
        query.order_by(GameSession.started_at.desc(), GameSession.id.desc()).limit(
            limit + 1
        )
    ).all()
    page = sessions[:limit]
    events, truncated = (
        _journal_events(
            session, campaign_id, [row.id for row in page], viewer, member, view
        )
        if page
        else ([], {})
    )
    grants, entities = _journal_context(
        session, campaign_id, viewer, member, events, view
    )
    by_session = {row.id: [] for row in page}
    for event in events:
        by_session[event.session_id].append(event)
    views = []
    for row in page:
        entry = journal(
            viewer,
            member,
            by_session[row.id],
            current_grants=grants,
            visible_entities=entities,
            view=view,
        )
        entry.truncated = truncated.get(row.id, False)
        views.append(
            SessionJournalView(
                session_id=row.id,
                started_at=row.started_at.replace(tzinfo=timezone.utc)
                if row.started_at.tzinfo is None
                else row.started_at,
                journal=entry,
            )
        )
    return CampaignJournalView(
        sessions=views,
        next_cursor=_journal_cursor(page[-1]) if len(sessions) > limit else None,
    )


@router.post(
    "/campaigns/{campaign_id}/sessions/{session_id}/rolls",
    response_model=SessionEventView,
    dependencies=[Depends(require_play_enabled), Depends(_require_roll_scope)],
)
def create_roll(
    campaign_id: str,
    session_id: str,
    body: RollRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
    rng: DiceRng = Depends(get_dice_rng),
) -> SessionEventView:
    member = _get_member_or_404(session, campaign_id, email)
    game_session = _session_in_campaign(session, campaign_id, session_id)
    visibility = body.visibility or ("dm" if member.role == "dm" else "table")
    if (
        member.role != "dm"
        and member.player_character_id is None
        and visibility != "table"
    ):
        raise HTTPException(
            status_code=422, detail="players without a character may roll only table"
        )
    if visibility == "table":
        audience = Audience("table")
    elif visibility == "dm" or member.role == "dm":
        audience = Audience("dm", author_member_id=member.id)
    else:
        audience = Audience(
            "pcs", frozenset({member.player_character_id}), author_member_id=member.id
        )
    try:
        result = roll(body.formula, rng)
    except DiceFormulaError as exc:
        raise HTTPException(status_code=400, detail=f"invalid formula: {exc}") from exc
    try:
        row = append_event(
            session,
            game_session=game_session,
            kind="roll",
            audience=audience,
            author_member_id=member.id,
            body={**result, "label": body.label, "visibility": visibility},
        )
    except SessionEndedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    session.refresh(row)
    return _event_view(row, member)


@router.post(
    "/campaigns/{campaign_id}/sessions/{session_id}/events",
    response_model=SessionEventView,
    dependencies=[Depends(require_play_enabled)],
)
def create_session_event(
    campaign_id: str,
    session_id: str,
    body: SessionEventRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> SessionEventView:
    member = _get_member_or_404(session, campaign_id, email)
    game_session = _session_in_campaign(session, campaign_id, session_id)
    if body.kind == "utterance":
        raise HTTPException(status_code=403, detail="utterances require ingest")
    if body.kind == "roll":
        raise HTTPException(
            status_code=403, detail="rolls require the server-side roller"
        )
    if member.role != "dm" and (
        body.kind != "action" or body.audience not in ("dm", "table")
    ):
        raise HTTPException(status_code=403, detail="player action required")
    try:
        audience = Audience(
            body.audience,
            frozenset(body.audience_pc_ids),
            author_member_id=member.id,
        )
        row = append_event(
            session,
            game_session=game_session,
            kind=body.kind,
            audience=audience,
            author_member_id=member.id,
            body=body.body,
            request_id=body.request_id,
        )
    except (SessionEndedError, EventRequestConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (InvalidEventAudienceError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    session.refresh(row)
    return _event_view(row, member)


@router.get(
    "/campaigns/{campaign_id}/sessions/{session_id}/events",
    response_model=list[SessionEventView],
    dependencies=[Depends(require_play_enabled)],
)
def list_session_events(
    campaign_id: str,
    session_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[SessionEventView]:
    member = _get_member_or_404(session, campaign_id, email)
    _session_in_campaign(session, campaign_id, session_id)
    viewer = _viewer_for_member(session, campaign_id, member)
    rows = session.exec(
        select(SessionEvent)
        .where(
            SessionEvent.campaign_id == campaign_id,
            SessionEvent.session_id == session_id,
            SessionEvent.seq > after,
            audience_predicate(SessionEvent, viewer, member),
        )
        .order_by(SessionEvent.seq)
        .limit(limit)
    ).all()
    return [_event_view(row, member) for row in rows]


@router.post(
    "/campaigns/{campaign_id}/sessions/{session_id}/events/{event_id}/retract",
    response_model=SessionEventView,
    dependencies=[Depends(require_play_enabled)],
)
def retract_session_event(
    campaign_id: str,
    session_id: str,
    event_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> SessionEventView:
    member = _get_member_or_404(session, campaign_id, email)
    _session_in_campaign(session, campaign_id, session_id)
    row = session.exec(
        select(SessionEvent)
        .where(
            SessionEvent.id == event_id,
            SessionEvent.session_id == session_id,
            SessionEvent.campaign_id == campaign_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="session event not found")
    if member.role != "dm" and (
        row.kind != "action" or row.author_member_id != member.id
    ):
        raise HTTPException(status_code=403, detail="own player action required")
    # Retraction is idempotent and permitted after a session ends.
    if row.retracted_at is None:
        row.retracted_at = datetime.now(timezone.utc)
        sync_event_embeddings(session, row)
        session.commit()
        session.refresh(row)
    return _event_view(row, member)


# --- Registered-user lobby and accepted invitations --------------------


class LobbyUserView(BaseModel):
    id: str
    email: str
    display_name: str | None


class LobbyCampaignView(CampaignView):
    role: MemberRole
    is_owner: bool
    player_character_id: str | None
    character_name: str | None


class InvitationView(BaseModel):
    invitee_email: str
    id: str
    campaign_id: str
    campaign_name: str
    status: str


class LobbyView(BaseModel):
    can_administer_accounts: bool
    invitation_links_enabled: bool = False
    invitation_enrollment_enabled: bool = False
    can_create_game: bool = True
    user: LobbyUserView
    campaigns: list[LobbyCampaignView]
    invitations: list[InvitationView]


def _registered_user(session: Session, email: str) -> AppUser:
    user = _request_user(session, email)
    if user is None or user.issuer is None:
        raise HTTPException(403, detail="sign in to register with Grimoire first")
    return user


def _invitation_view(
    session: Session, invitation: CampaignInvitation
) -> InvitationView:
    campaign = _get_campaign_or_404(session, invitation.campaign_id)
    return InvitationView(
        invitee_email=session.get(AppUser, invitation.invitee_id).email,
        id=invitation.id,
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        status=invitation.status,
    )


@router.get("/lobby", response_model=LobbyView)
def get_lobby(
    principal: Principal = Depends(get_authenticated_identity),
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> LobbyView:
    user = _registered_user(session, email)
    memberships = session.exec(
        select(CampaignMember).where(
            CampaignMember.app_user_id == user.id,
        )
    ).all()
    campaigns = []
    for member in memberships:
        campaign = _get_campaign_or_404(session, member.campaign_id)
        campaigns.append(
            LobbyCampaignView(
                id=campaign.id,
                name=campaign.name,
                dm_name=campaign.dm_name,
                created_at=campaign.created_at,
                notes_dm_readable_default=campaign.notes_dm_readable_default,
                role=member.role,
                is_owner=campaign.owner_app_user_id == user.id,
                player_character_id=member.player_character_id,
                character_name=_member_character_name(session, member),
            )
        )
    invitations = session.exec(
        select(CampaignInvitation)
        .where(
            CampaignInvitation.invitee_id == user.id,
            CampaignInvitation.status == "pending",
        )
        .order_by(CampaignInvitation.created_at)
    ).all()
    from auth.api import platform_enforcement_enabled, require_application_permission

    can_create = True
    if platform_enforcement_enabled():
        try:
            require_application_permission(session, principal, "grimoire.create_game")
        except HTTPException:
            can_create = False
    return LobbyView(
        can_create_game=can_create,
        invitation_links_enabled=links_enabled(),
        invitation_enrollment_enabled=enrollment_enabled(),
        can_administer_accounts=principal.has_group("operators"),
        user=LobbyUserView(
            id=user.id, email=user.email, display_name=user.display_name
        ),
        campaigns=campaigns,
        invitations=[_invitation_view(session, row) for row in invitations],
    )


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=320)


@router.post("/campaigns/{campaign_id}/invitations", response_model=InvitationView)
def invite_registered_player(
    campaign_id: str,
    body: InviteRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> InvitationView:
    owner = _require_owner(session, campaign_id, email)
    # Serialize invitations and membership changes for this campaign.
    session.exec(
        select(Campaign).where(Campaign.id == campaign_id).with_for_update()
    ).one()
    recipient = find_registered_user(session, body.email)
    if recipient is None:
        raise HTTPException(
            404, detail="no registered player with that email or username"
        )
    existing = session.exec(
        select(CampaignMember).where(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.app_user_id == recipient.id,
        )
    ).first()
    if existing is not None:
        raise HTTPException(409, detail="player is already a campaign member")
    invitation = session.exec(
        select(CampaignInvitation)
        .where(
            CampaignInvitation.campaign_id == campaign_id,
            CampaignInvitation.invitee_id == recipient.id,
        )
        .with_for_update()
    ).first()
    if invitation is None:
        invitation = CampaignInvitation(
            campaign_id=campaign_id, invitee_id=recipient.id, invited_by_id=owner.id
        )
    elif invitation.status != "pending":
        # A fresh ID prevents an old accept request from consuming a new invite.
        session.delete(invitation)
        session.flush()
        invitation = CampaignInvitation(
            campaign_id=campaign_id, invitee_id=recipient.id, invited_by_id=owner.id
        )
    session.add(invitation)
    session.commit()
    session.refresh(invitation)
    return _invitation_view(session, invitation)


@router.get("/campaigns/{campaign_id}/invitations", response_model=list[InvitationView])
def list_sent_invitations(
    campaign_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> list[InvitationView]:
    _require_owner(session, campaign_id, email)
    rows = session.exec(
        select(CampaignInvitation).where(
            CampaignInvitation.campaign_id == campaign_id,
            CampaignInvitation.status == "pending",
        )
    ).all()
    return [_invitation_view(session, row) for row in rows]


@router.delete("/campaigns/{campaign_id}/invitations/{invitation_id}", status_code=204)
def cancel_invitation(
    campaign_id: str,
    invitation_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> None:
    _require_owner(session, campaign_id, email)
    row = session.exec(
        select(CampaignInvitation)
        .where(
            CampaignInvitation.id == invitation_id,
            CampaignInvitation.campaign_id == campaign_id,
        )
        .with_for_update()
    ).first()
    if row is None:
        raise HTTPException(404, detail="invitation not found")
    if row.status != "pending":
        raise HTTPException(409, detail="invitation is no longer pending")
    row.status = "revoked"
    session.add(row)
    session.commit()


@router.post("/invitations/{invitation_id}/{decision}", response_model=InvitationView)
def decide_invitation(
    invitation_id: str,
    decision: Literal["accept", "decline"],
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> InvitationView:
    user = _registered_user(session, email)
    row = session.exec(
        select(CampaignInvitation)
        .where(
            CampaignInvitation.id == invitation_id,
            CampaignInvitation.invitee_id == user.id,
        )
        .with_for_update()
    ).first()
    if row is None:
        raise HTTPException(404, detail="invitation not found")
    if row.status != "pending":
        raise HTTPException(409, detail="invitation is no longer pending")
    if decision == "accept":
        existing = session.exec(
            select(CampaignMember).where(
                CampaignMember.campaign_id == row.campaign_id,
                CampaignMember.app_user_id == user.id,
            )
        ).first()
        if existing is not None:
            raise HTTPException(409, detail="already a campaign member")
        session.add(
            CampaignMember(
                campaign_id=row.campaign_id, app_user_id=user.id, role="player"
            )
        )
        row.status = "accepted"
    else:
        row.status = "declined"
    session.add(row)
    session.commit()
    return _invitation_view(session, row)


# Specific public-capability endpoints stay behind their own default-off gate.
router.include_router(join_links_router)
