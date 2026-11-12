"""In-app feedback from the friends web app: intake, triage and the roadmap.

Every signed-in user reads the roadmap, so a friend can see what was already
suggested and what happened to it. The sender's identity, page and campaign
stay with operators. The repository is public, so nothing is published
automatically: filing sends GitHub only the title and body the operator wrote,
a fixed footer and fixed labels.
"""

import logging
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

import httpx
from auth.api import Principal
from core.clock import aware, utc_now
from core.db import get_session
from core.github import GITHUB_REPO, create_issue, list_issue_states
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, or_, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, select

from grimoire.access import get_authenticated_email, get_authenticated_identity
from grimoire.models import Campaign, Feedback, FeedbackStatus, FeedbackVote

logger = logging.getLogger("monolith.grimoire.feedback")

DAILY_LIMIT = 10
LIST_LIMIT = 100
ROADMAP_LIMIT = 200
ISSUE_REFRESH_INTERVAL = timedelta(minutes=10)
UNTITLED = "Suggestion"
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
    # A pathname from the app, without a host, query, fragment or controls.
    page: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^/([^\x00-\x20/\\?#][^\x00-\x20\\?#]*)?$",
    )
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
    votes: int


RoadmapState = Literal["new", "accepted", "completed", "rejected"]


class RoadmapItem(BaseModel):
    """What every signed-in user sees. No author, page or campaign."""

    id: str
    state: RoadmapState
    text: str
    votes: int
    voted: bool
    mine: bool
    created_at: datetime
    issue_url: str | None
    reason: str | None


class VoteReceipt(BaseModel):
    votes: int
    voted: bool


class DismissRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # One line, shown on the roadmap. Control characters include NUL.
    reason: str | None = Field(
        default=None, max_length=200, pattern=r"^[^\x00-\x1f\x7f]*$"
    )


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
    # Serialize one sender's submissions so parallel requests cannot all read
    # the same count and overshoot the limit. Session autobegin holds this
    # transaction lock through count/insert until commit (or rollback/close
    # on rejection). PostgreSQL's default READ COMMITTED gives the count a
    # fresh snapshot after a waiting request acquires the lock.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"grimoire-feedback:{email}"},
        )
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
        author_email=email,
        campaign_id=campaign_id,
        page=body.page,
        text=body.text,
        shared=True,
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
        select(Feedback, Campaign.name, _vote_count())
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
            votes=votes,
        )
        for row, campaign_name, votes in rows
    ]


def _vote_count():
    return (
        select(func.count())
        .select_from(FeedbackVote)
        .where(FeedbackVote.feedback_id == Feedback.id)
        .correlate(Feedback)
        .scalar_subquery()
    )


def _issue_url(number: int) -> str:
    return f"https://github.com/{GITHUB_REPO}/issues/{number}"


def _state(row: Feedback) -> RoadmapState:
    if row.status == "new":
        return "new"
    if row.status == "dismissed" or row.issue_state == "not_planned":
        return "rejected"
    # A filed row not yet read back from GitHub counts as open.
    return "completed" if row.issue_state == "completed" else "accepted"


def _visible(email: str):
    """Shared notes, the caller's own, and anything filed (by its title)."""
    return or_(
        Feedback.shared, Feedback.author_email == email, Feedback.status == "filed"
    )


def _refresh_issue_states(session: Session) -> None:
    """Read issue states back from GitHub, at most once per interval.

    One request refreshes while the others serve what is stored. A failed
    attempt is stamped too, so an outage costs one bounded call per interval.
    """
    if session.get_bind().dialect.name == "postgresql":
        held = session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
            {"key": "grimoire-feedback-roadmap"},
        ).scalar_one()
        if not held:
            return
    now = utc_now()
    stale = session.exec(
        select(Feedback.id)
        .where(
            Feedback.status == "filed",
            or_(
                Feedback.issue_checked_at.is_(None),
                Feedback.issue_checked_at < now - ISSUE_REFRESH_INTERVAL,
            ),
        )
        .limit(1)
    ).first()
    if stale is None:
        return
    try:
        states = list_issue_states(
            GITHUB_REPO, list(ISSUE_LABELS), user_agent="monolith-grimoire-feedback"
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "grimoire feedback: issue states not refreshed (%s)", type(exc).__name__
        )
        states = {}
    filed = Feedback.status == "filed"
    session.execute(update(Feedback).where(filed).values(issue_checked_at=now))
    for state in ("open", "completed", "not_planned"):
        numbers = [number for number, value in states.items() if value == state]
        if numbers:
            session.execute(
                update(Feedback)
                .where(filed, Feedback.issue_number.in_(numbers))
                .values(issue_state=state)
            )
    session.commit()


@router.get("/roadmap", response_model=list[RoadmapItem])
def read_roadmap(
    _principal: Identity, email: AuthEmail, session: DatabaseSession
) -> list[RoadmapItem]:
    try:
        _refresh_issue_states(session)
    except SQLAlchemyError as exc:
        session.rollback()
        logger.warning(
            "grimoire feedback: issue states not saved (%s)", type(exc).__name__
        )
    rows = session.exec(
        select(Feedback, _vote_count())
        .where(_visible(email))
        .order_by(Feedback.created_at.desc(), Feedback.id)
        .limit(ROADMAP_LIMIT)
    ).all()
    voted = set(
        session.exec(
            select(FeedbackVote.feedback_id).where(
                FeedbackVote.voter_email == email,
                FeedbackVote.feedback_id.in_([row.id for row, _votes in rows]),
            )
        ).all()
    )
    items = []
    for row, votes in rows:
        mine = row.author_email == email
        state = _state(row)
        # An unshared note's words are shown only to the person who wrote them.
        if row.title:
            shown = row.title
        elif row.shared or mine:
            shown = row.text
        else:
            shown = UNTITLED
        items.append(
            RoadmapItem(
                id=row.id,
                state=state,
                text=shown,
                votes=votes,
                voted=row.id in voted,
                mine=mine,
                created_at=aware(row.created_at),
                issue_url=_issue_url(row.issue_number) if row.issue_number else None,
                reason=row.reason if state == "rejected" else None,
            )
        )
    return items


def _votable(session: Session, feedback_id: UUID, email: str) -> Feedback:
    row = session.exec(
        select(Feedback).where(Feedback.id == str(feedback_id), _visible(email))
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="feedback not found")
    if row.author_email == email:
        raise HTTPException(status_code=409, detail="This one is yours.")
    if _state(row) not in ("new", "accepted"):
        raise HTTPException(status_code=409, detail="Voting has closed on this one.")
    return row


def _vote_receipt(session: Session, row_id: str, voted: bool) -> VoteReceipt:
    votes = session.exec(
        select(func.count())
        .select_from(FeedbackVote)
        .where(FeedbackVote.feedback_id == row_id)
    ).one()
    return VoteReceipt(votes=votes, voted=voted)


@router.put("/{feedback_id}/vote", response_model=VoteReceipt)
def vote_for_feedback(
    feedback_id: UUID, _principal: Identity, email: AuthEmail, session: DatabaseSession
) -> VoteReceipt:
    row_id = _votable(session, feedback_id, email).id
    if session.get(FeedbackVote, (row_id, email)) is None:
        session.add(FeedbackVote(feedback_id=row_id, voter_email=email))
        try:
            session.commit()
        except IntegrityError:
            # A parallel request stored the same vote first.
            session.rollback()
    return _vote_receipt(session, row_id, True)


@router.delete("/{feedback_id}/vote", response_model=VoteReceipt)
def withdraw_vote(
    feedback_id: UUID, _principal: Identity, email: AuthEmail, session: DatabaseSession
) -> VoteReceipt:
    row_id = _votable(session, feedback_id, email).id
    session.execute(
        delete(FeedbackVote).where(
            FeedbackVote.feedback_id == row_id, FeedbackVote.voter_email == email
        )
    )
    session.commit()
    return _vote_receipt(session, row_id, False)


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
    row.title = body.title
    row.issue_state = "open"
    row.resolved_at = utc_now()
    try:
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        logger.warning(
            "grimoire feedback: issue published but resolution unconfirmed (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail="GitHub created the issue but saving its resolution failed. "
            "Check the repository before filing again.",
        ) from None
    return FeedbackIssue(
        issue_number=number,
        issue_url=_issue_url(number),
    )


@router.post("/{feedback_id}/dismiss", status_code=204)
def dismiss_feedback(
    feedback_id: UUID,
    _operator: Operator,
    session: DatabaseSession,
    body: DismissRequest | None = None,
) -> None:
    row = _new_row(session, feedback_id)
    row.status = "dismissed"
    row.reason = (body.reason if body else None) or None
    row.resolved_at = utc_now()
    session.commit()
