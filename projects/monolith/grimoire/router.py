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

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from auth.api import Authority, Principal, PrincipalKind, get_principal
from core.db import get_session
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from knowledge.api import get_embedding_client
from pydantic import BaseModel, ConfigDict, Field, field_validator
from shared.embedding import EmbeddingClient
from sqlalchemy import func
from sqlmodel import Session, or_, select

from grimoire import aliases, library
from grimoire.access import (
    get_authenticated_email,
    get_authenticated_identity,
    get_grimoire_operator_email,
)
from grimoire.models import (
    ENTITY_DETAIL_MODELS,
    AppUser,
    Campaign,
    CampaignMember,
    CampaignInvitation,
    CharacterSheetStatus,
    CharacterSheetVersion,
    Entity,
    EntityType,
    GameSession,
    GrantScope,
    KnowledgeChunk,
    KnowledgeGrant,
    MemberRole,
    PlayerCharacter,
    Relationship,
    SessionStatus,
)
from grimoire.sheets import CharacterSheetV1, SheetValidationError, derive_sheet
from grimoire.search import search_campaign
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
    email: str = Depends(get_authenticated_email),
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


class MemberView(BaseModel):
    id: str
    email: str
    role: MemberRole
    player_character_id: str | None
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
        created_at=member.created_at,
    )


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


class GrantView(BaseModel):
    id: str
    campaign_id: str
    entity_id: str
    player_character_id: str
    grant_scope: GrantScope
    revealed_details: dict | None
    granted_in_session: str | None
    created_at: datetime


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


@router.post("/campaigns/{campaign_id}/grants", response_model=GrantView)
def create_grant(
    campaign_id: str,
    body: GrantCreateRequest,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> KnowledgeGrant:
    _require_dm(session, campaign_id, email)
    _get_character_in_campaign_or_404(session, campaign_id, body.player_character_id)
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

    grant = KnowledgeGrant(
        campaign_id=campaign_id,
        entity_id=body.entity_id,
        player_character_id=body.player_character_id,
        grant_scope=body.grant_scope,
        revealed_details=body.revealed_details,
        granted_in_session=body.granted_in_session,
    )
    session.add(grant)
    session.commit()
    session.refresh(grant)
    return grant


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
    _require_dm(session, campaign_id, email)
    grant = session.get(KnowledgeGrant, grant_id)
    if grant is None or grant.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="grant not found")

    if body.grant_scope is not None:
        grant.grant_scope = body.grant_scope
    if body.revealed_details is not None:
        grant.revealed_details = body.revealed_details

    session.add(grant)
    session.commit()
    session.refresh(grant)
    return grant


@router.delete("/campaigns/{campaign_id}/grants/{grant_id}", status_code=204)
def delete_grant(
    campaign_id: str,
    grant_id: str,
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> None:
    """Revoke a grant (the grant editor's "none" scope). Idempotent-ish: a
    missing grant is a 404, matching update_grant's not-found semantics. Removing
    a grant on a non-global entity returns it to invisible for that character;
    on a global entity it drops the character back to the default full view."""
    _require_dm(session, campaign_id, email)
    grant = session.get(KnowledgeGrant, grant_id)
    if grant is None or grant.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="grant not found")
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


@router.get("/campaigns/{campaign_id}/entities")
def list_entities(
    campaign_id: str,
    entity_type: EntityType | None = Query(default=None, alias="type"),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Grant-filtered, paginated entity list, spine-level only (no typed detail).

    Returns ``{items, total, next_cursor}``. Item dict shape is scope-dependent
    (full spine, partial identity + revealed_details, or a full spine + grants
    annotation for the DM), so items are plain dicts rather than a fixed
    response_model, matching the heterogeneous-payload pattern used by
    knowledge/router.py.

    The projection (DM grant-aggregation, player grant-filtering, name_only
    suppression) has to run in Python before the page is known, so the full
    visible set is materialised and then sliced by ``cursor``/``limit``. At v1
    corpus scale (hundreds of entities) this stays cheap; keyset pagination
    would have to reconcile the DM view's one-row-per-grant fan-out and is not
    worth it yet.
    """
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)

    query = visible_entities_query(campaign_id, viewer).order_by(Entity.name)
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
    email: str = Depends(get_authenticated_email),
    session: Session = Depends(get_session),
    embed_client: EmbeddingClient = Depends(get_embedding_client),
) -> list[dict[str, Any]]:
    """kNN search over embedding, grant-filtered, mixed entity/chunk hits.

    All visibility filtering happens in search.search_campaign, which builds
    on the same visible_entities_query()/project_entity() helpers as the
    entity read paths above.
    """
    member = _get_member_or_404(session, campaign_id, email)
    viewer = _viewer_for_member(session, campaign_id, member)
    return await search_campaign(session, embed_client, campaign_id, viewer, q, k=k)


# --- Game sessions ---------------------------------------------------


class GameSessionUpdateRequest(BaseModel):
    status: SessionStatus


class GameSessionView(BaseModel):
    id: str
    campaign_id: str
    status: SessionStatus
    started_at: datetime
    ended_at: datetime | None


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


# --- Registered-user lobby and accepted invitations --------------------


class LobbyUserView(BaseModel):
    id: str
    email: str
    display_name: str | None


class LobbyCampaignView(CampaignView):
    role: MemberRole
    is_owner: bool


class InvitationView(BaseModel):
    invitee_email: str
    id: str
    campaign_id: str
    campaign_name: str
    status: str


class LobbyView(BaseModel):
    can_administer_accounts: bool
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
                role=member.role,
                is_owner=campaign.owner_app_user_id == user.id,
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
    return LobbyView(
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
    recipient = session.exec(
        select(AppUser).where(
            AppUser.email == body.email.lower(),
            AppUser.issuer.is_not(None),
        )
    ).first()
    if recipient is None:
        raise HTTPException(404, detail="no registered player with that email")
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
