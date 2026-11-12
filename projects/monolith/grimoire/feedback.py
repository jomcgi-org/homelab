"""In-app feedback from the friends web app, triaged privately by an operator.

A friend's text and identity stay in ``grimoire.feedback``. The repository is
public, so nothing is published automatically: filing sends GitHub only the
title and body the operator wrote, a fixed footer and fixed labels.
"""

import logging
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

import httpx
from auth.api import Principal
from core.clock import aware, utc_now
from core.db import get_session
from core.github import GITHUB_REPO, create_issue
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlmodel import Session, select

from grimoire.access import get_authenticated_email, get_authenticated_identity
from grimoire.models import Campaign, Feedback, FeedbackStatus

logger = logging.getLogger("monolith.grimoire.feedback")

DAILY_LIMIT = 10
LIST_LIMIT = 100
ISSUE_LABELS = ("grimoire", "feedback")
ISSUE_FOOTER = "Filed from Grimoire feedback."

router = APIRouter(prefix="/api/grimoire/feedback", tags=["grimoire"])
DatabaseSession = Annotated[Session, Depends(get_session)]
Identity = Annotated[Principal, Depends(get_authenticated_identity)]
AuthEmail = Annotated[str, Depends(get_authenticated_email)]


def _operator(principal: Identity, _email: AuthEmail) -> Principal:
    """The lobby's `can_administer_accounts` rule, enforced per request."""
    if not principal.has_group("operators"):
        raise HTTPException(status_code=403, detail="operator access required")
    return principal


Operator = Annotated[Principal, Depends(_operator)]


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # Postgres text cannot hold NUL, so it is refused here as a 422.
    text: str = Field(min_length=1, max_length=2000, pattern=r"^[^\x00]*$")
    page: str = Field(min_length=1, max_length=200, pattern=r"^/[^\x00]*$")
    campaign_id: UUID | None = None


class FeedbackReceipt(BaseModel):
    id: str


class FeedbackView(BaseModel):
    id: str
    created_at: datetime
    author: str
    campaign_id: str | None
    campaign_name: str | None
    page: str
    text: str
    status: FeedbackStatus
    issue_number: int | None


class FeedbackIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=4000)


class FeedbackIssue(BaseModel):
    issue_number: int
    issue_url: str


def new_feedback_count(session: Session) -> int:
    return session.exec(
        select(func.count()).select_from(Feedback).where(Feedback.status == "new")
    ).one()


@router.post("", response_model=FeedbackReceipt, status_code=201)
def submit_feedback(
    body: FeedbackRequest,
    _principal: Identity,
    email: AuthEmail,
    session: DatabaseSession,
) -> FeedbackReceipt:
    # The campaign is context for triage. A sender who is not a member (a
    # stale link, a removed player) still gets heard, without the campaign.
    campaign_id = None
    if body.campaign_id is not None:
        from grimoire.router import _get_member_or_404

        try:
            _get_member_or_404(session, str(body.campaign_id), email)
            campaign_id = str(body.campaign_id)
        except HTTPException:
            campaign_id = None
    recent = session.exec(
        select(func.count())
        .select_from(Feedback)
        .where(
            Feedback.author_email == email,
            Feedback.created_at > utc_now() - timedelta(hours=24),
        )
    ).one()
    if recent >= DAILY_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"You have sent {DAILY_LIMIT} notes in the last day. "
            "Try again tomorrow.",
        )
    row = Feedback(
        author_email=email, campaign_id=campaign_id, page=body.page, text=body.text
    )
    session.add(row)
    session.commit()
    return FeedbackReceipt(id=row.id)


@router.get("", response_model=list[FeedbackView])
def list_feedback(
    _operator: Operator,
    session: DatabaseSession,
    status: FeedbackStatus = "new",
) -> list[FeedbackView]:
    rows = session.exec(
        select(Feedback, Campaign.name)
        .outerjoin(Campaign, Campaign.id == Feedback.campaign_id)
        .where(Feedback.status == status)
        .order_by(Feedback.created_at.desc(), Feedback.id)
        .limit(LIST_LIMIT)
    ).all()
    return [
        FeedbackView(
            id=row.id,
            created_at=aware(row.created_at),
            author=row.author_email,
            campaign_id=row.campaign_id,
            campaign_name=campaign_name,
            page=row.page,
            text=row.text,
            status=row.status,
            issue_number=row.issue_number,
        )
        for row, campaign_name in rows
    ]


def _new_row(session: Session, feedback_id: UUID) -> Feedback:
    """Lock the row so two triage requests cannot both act on it."""
    row = session.exec(
        select(Feedback).where(Feedback.id == str(feedback_id)).with_for_update()
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="feedback not found")
    if row.status != "new":
        raise HTTPException(status_code=409, detail=f"feedback is already {row.status}")
    return row


@router.post("/{feedback_id}/file", response_model=FeedbackIssue)
def file_feedback(
    feedback_id: UUID,
    body: FeedbackIssueRequest,
    _operator: Operator,
    session: DatabaseSession,
) -> FeedbackIssue:
    row = _new_row(session, feedback_id)
    # Only the operator's words are published. Nothing is read from the row.
    try:
        number = create_issue(
            GITHUB_REPO,
            body.title,
            f"{body.body}\n\n{ISSUE_FOOTER}",
            list(ISSUE_LABELS),
            user_agent="monolith-grimoire-feedback",
        )
    except (httpx.HTTPError, ValueError) as exc:
        session.rollback()
        logger.warning("grimoire feedback: issue not filed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="GitHub did not confirm the issue. "
            "Check the repository before filing again.",
        ) from None
    row.status = "filed"
    row.issue_number = number
    row.resolved_at = utc_now()
    session.commit()
    return FeedbackIssue(
        issue_number=number,
        issue_url=f"https://github.com/{GITHUB_REPO}/issues/{number}",
    )


@router.post("/{feedback_id}/dismiss", status_code=204)
def dismiss_feedback(
    feedback_id: UUID,
    _operator: Operator,
    session: DatabaseSession,
) -> None:
    row = _new_row(session, feedback_id)
    row.status = "dismissed"
    row.resolved_at = utc_now()
    session.commit()
