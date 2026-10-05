"""Single-recipient campaign capabilities, consumed with membership atomically.

The browser carries a random secret in a fragment, then an HttpOnly cookie.
Only its SHA-256 digest is persisted. All views are explicit allowlists.
"""

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from uuid import uuid4

from auth.api import (
    Principal,
    find_application_user_by_username,
    platform_enforcement_enabled,
)
from core.db import get_session
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, select

from grimoire.access import get_authenticated_email, get_authenticated_identity
from grimoire.invitation_provider import InvitationProvider, enrollment_enabled
from grimoire.models import AppUser, Campaign, CampaignJoinLink, CampaignMember

TTL = timedelta(days=7)
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def links_enabled() -> bool:
    return os.getenv("GRIMOIRE_INVITATION_LINKS_ENABLED", "") == "true"


def require_enabled():
    if not links_enabled():
        raise HTTPException(503, "Invitation links are not enabled yet.")


router = APIRouter(dependencies=[Depends(require_enabled)])
DatabaseSession = Annotated[Session, Depends(get_session)]
Identity = Annotated[Principal, Depends(get_authenticated_identity)]
AuthEmail = Annotated[str, Depends(get_authenticated_email)]
TokenBody = Annotated[Any, Body()]


class LinkView(BaseModel):
    id: str
    campaign_id: str
    campaign_name: str
    invitee_email: str
    expires_at: datetime
    status: str
    enrollment_cleanup_pending: bool = False


class IssuedLink(LinkView):
    token: str = Field(repr=False)


class InspectedLink(LinkView):
    can_enroll: bool


class JoinResult(BaseModel):
    campaign_id: str
    status: str = "accepted"


class IssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=320)
    allow_enrollment: bool = False


def _body_token(body: Any) -> str:
    # Never let Pydantic echo capability-bearing input inside a 422 error.
    if (
        not isinstance(body, dict)
        or set(body) != {"token"}
        or not isinstance(body["token"], str)
    ):
        raise HTTPException(400, "Invalid invitation request.")
    return body["token"]


def _now():
    return datetime.now(timezone.utc)


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _digest(token: str) -> str:
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise HTTPException(404, "Invitation not found.")
    return hashlib.sha256(token.encode()).hexdigest()


def _campaign_lock(session: Session, campaign_id: str) -> Campaign:
    # NO KEY UPDATE serializes our membership mutators without conflicting
    # with legacy invitation INSERT's campaign foreign-key KEY SHARE lock.
    campaign = session.exec(
        select(Campaign)
        .where(Campaign.id == campaign_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).first()
    if campaign is None:
        raise HTTPException(404, "Invitation not found.")
    return campaign


def _owner_lock(session: Session, campaign_id: str, owner: AppUser) -> Campaign:
    campaign = _campaign_lock(session, campaign_id)
    if campaign.owner_app_user_id != owner.id:
        raise HTTPException(403, "Campaign owner required.")
    return campaign


def _token_row(session: Session, token: str, *, lock=False) -> CampaignJoinLink:
    digest = _digest(token)
    row = session.exec(
        select(CampaignJoinLink).where(CampaignJoinLink.token_digest == digest)
    ).first()
    if row is None:
        raise HTTPException(404, "Invitation not found.")
    if lock:
        _campaign_lock(session, row.campaign_id)
        # Refresh after waiting: SQLAlchemy may otherwise return stale fields
        # from the identity map populated by the first lookup.
        row = session.exec(
            select(CampaignJoinLink)
            .where(CampaignJoinLink.id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
    return row


def _pending(row: CampaignJoinLink):
    if row.status != "pending":
        raise HTTPException(409, "This invitation is no longer available.")
    if _utc(row.expires_at) <= _now():
        raise HTTPException(410, "This invitation has expired. Ask for a new link.")


def _view(session: Session, row: CampaignJoinLink) -> LinkView:
    campaign = session.get(Campaign, row.campaign_id)
    return LinkView(
        id=row.id,
        campaign_id=row.campaign_id,
        campaign_name=campaign.name,
        invitee_email=row.invitee_email,
        expires_at=_utc(row.expires_at),
        status="expired"
        if row.status == "pending" and _utc(row.expires_at) <= _now()
        else row.status,
        enrollment_cleanup_pending=row.status == "revoked"
        and row.enrollment_id is not None,
    )


def issue_link(
    session: Session,
    campaign_id: str,
    owner: AppUser,
    email: str,
    *,
    allow_enrollment=False,
    can_administer_accounts=False,
) -> IssuedLink:
    _owner_lock(session, campaign_id, owner)
    email = email.strip().lower()
    if (
        not re.fullmatch(
            r"(?:[^\s@]+@[^\s@]+\.[^\s@]+|@[a-z0-9][a-z0-9_.-]{2,31})", email
        )
        or len(email) > 320
    ):
        raise HTTPException(400, "Enter the player's email or @username.")
    if email.startswith("@") and platform_enforcement_enabled():
        app_id = find_application_user_by_username(session, "grimoire", email[1:])
        recipient = session.get(AppUser, app_id) if app_id else None
    else:
        recipient = session.exec(
            select(AppUser).where(AppUser.email == email, AppUser.issuer.is_not(None))
        ).first()
    if recipient is None:
        if not allow_enrollment or not can_administer_accounts:
            raise HTTPException(
                404,
                "This player must sign in to Grimoire first, or ask an account administrator for an enrollment invitation.",
            )
        if not enrollment_enabled():
            raise HTTPException(
                503, "Account enrollment is not enabled. Ask an administrator."
            )
        # Validate complete configuration before promising a usable link.
        InvitationProvider()
    elif session.exec(
        select(CampaignMember).where(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.app_user_id == recipient.id,
        )
    ).first():
        raise HTTPException(409, "This player is already a campaign member.")
    # A retry returns no old token and creates no second active invitation.
    # The owner can revoke the pending row, then explicitly create a new link.
    pending = session.exec(
        select(CampaignJoinLink).where(
            CampaignJoinLink.campaign_id == campaign_id,
            CampaignJoinLink.invitee_email == email,
            CampaignJoinLink.status == "pending",
            CampaignJoinLink.expires_at > _now(),
        )
    ).first()
    if pending:
        raise HTTPException(
            409,
            "A pending invitation already exists. Revoke it before creating a replacement.",
        )
    token = secrets.token_urlsafe(32)
    link_id = str(uuid4())
    row = CampaignJoinLink(
        id=link_id,
        campaign_id=campaign_id,
        recipient_id=recipient.id if recipient else None,
        invitee_email=email,
        issued_by_id=owner.id,
        token_digest=_digest(token),
        expires_at=_now() + TTL,
        enrollment_allowed=recipient is None,
        enrollment_username=f"grimoire-{hashlib.sha256(email.encode()).hexdigest()}",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return IssuedLink(**_view(session, row).model_dump(), token=token)


def redeem_link(session: Session, token: str, user: AppUser) -> JoinResult:
    row = _token_row(session, token, lock=True)
    if row.recipient_id is not None:
        matches = row.recipient_id == user.id
    else:
        issuer = os.getenv("GRIMOIRE_AUTH_ISSUER", "")
        matches = bool(
            issuer and user.issuer == issuer and user.email == row.invitee_email
        )
    if not matches:
        raise HTTPException(
            403, "Sign in with the account this invitation was created for."
        )
    member = session.exec(
        select(CampaignMember).where(
            CampaignMember.campaign_id == row.campaign_id,
            CampaignMember.app_user_id == user.id,
        )
    ).first()
    if row.status == "accepted" and row.accepted_by_id == user.id and member:
        return JoinResult(campaign_id=row.campaign_id)
    _pending(row)
    if member:
        raise HTTPException(409, "You are already a campaign member.")
    session.add(
        CampaignMember(campaign_id=row.campaign_id, app_user_id=user.id, role="player")
    )
    row.status = "accepted"
    row.recipient_id = user.id
    row.accepted_by_id = user.id
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "Membership changed. Please retry joining.") from None
    return JoinResult(campaign_id=row.campaign_id)


def revoke_link(
    session: Session, campaign_id: str, link_id: str, owner: AppUser
) -> None:
    _owner_lock(session, campaign_id, owner)
    row = session.exec(
        select(CampaignJoinLink)
        .where(
            CampaignJoinLink.id == link_id, CampaignJoinLink.campaign_id == campaign_id
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        raise HTTPException(404, "Invitation not found.")
    if row.status == "accepted":
        raise HTTPException(
            409, "This player already joined. Use Remove player instead."
        )
    row.status = "revoked"
    session.add(row)
    # Revoke app capability even if Authentik is unavailable. Preserve provider
    # ID so repeating Revoke can finish cancellation without restoring access.
    session.commit()
    if row.enrollment_id:
        InvitationProvider().revoke(row.enrollment_id)
        row.enrollment_id = None
        session.add(row)
        session.commit()


@router.post("/campaigns/{campaign_id}/join-links", response_model=IssuedLink)
def issue(
    campaign_id: str,
    body: IssueRequest,
    principal: Identity,
    email: AuthEmail,
    session: DatabaseSession,
):
    from grimoire.router import _require_owner

    owner = _require_owner(session, campaign_id, email)
    return issue_link(
        session,
        campaign_id,
        owner,
        body.email,
        allow_enrollment=body.allow_enrollment,
        can_administer_accounts=principal.has_group("operators"),
    )


@router.get("/campaigns/{campaign_id}/join-links", response_model=list[LinkView])
def list_links(
    campaign_id: str,
    email: AuthEmail,
    session: DatabaseSession,
):
    from grimoire.router import _require_owner

    _require_owner(session, campaign_id, email)
    return [
        _view(session, row)
        for row in session.exec(
            select(CampaignJoinLink)
            .where(CampaignJoinLink.campaign_id == campaign_id)
            .order_by(CampaignJoinLink.created_at.desc())
        ).all()
    ]


@router.delete("/campaigns/{campaign_id}/join-links/{link_id}", status_code=204)
def revoke(
    campaign_id: str,
    link_id: str,
    email: AuthEmail,
    session: DatabaseSession,
):
    from grimoire.router import _require_owner

    revoke_link(
        session, campaign_id, link_id, _require_owner(session, campaign_id, email)
    )


@router.post("/join-links/inspect", response_model=InspectedLink)
def inspect(body: TokenBody, session: DatabaseSession):
    row = _token_row(session, _body_token(body))
    # Accepted metadata stays available for the final authenticated page's
    # idempotent retry. Revoked/expired links expose no recipient metadata.
    if row.status != "accepted":
        _pending(row)
    return InspectedLink(
        **_view(session, row).model_dump(),
        can_enroll=row.enrollment_allowed
        and row.status == "pending"
        and enrollment_enabled(),
    )


@router.post("/join-links/enroll")
def enroll(body: TokenBody, session: DatabaseSession):
    row = _token_row(session, _body_token(body), lock=True)
    _pending(row)
    if not row.enrollment_allowed:
        raise HTTPException(
            403, "This invitation is for an existing account. Please sign in."
        )
    if session.exec(
        select(AppUser).where(
            AppUser.email == row.invitee_email, AppUser.issuer.is_not(None)
        )
    ).first():
        raise HTTPException(409, "An account already exists. Please sign in.")
    provider = InvitationProvider()
    if not row.enrollment_id or not provider.exists(row.enrollment_id):
        row.enrollment_id = provider.create(
            link_id=row.id,
            email=row.invitee_email,
            username=row.enrollment_username,
            expires=_utc(row.expires_at),
        )
        session.add(row)
        try:
            session.commit()
        except SQLAlchemyError:
            # StatementError includes bound parameters, including Authentik's
            # secret invitation UUID. Do not let it reach generic 500 logging.
            session.rollback()
            raise HTTPException(
                503, "Account enrollment could not be saved. Please retry."
            ) from None
    return {"enrollment_url": provider.enrollment_url(row.enrollment_id)}


@router.post("/join-links/redeem", response_model=JoinResult)
def redeem(
    body: TokenBody,
    email: AuthEmail,
    session: DatabaseSession,
):
    from grimoire.router import _registered_user

    return redeem_link(session, _body_token(body), _registered_user(session, email))
