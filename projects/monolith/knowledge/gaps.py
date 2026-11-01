"""Gap discovery and review.

A gap is an unresolved body-text wikilink. Discovery writes unresolved terms
to Postgres, and review endpoints audit gap state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from sqlalchemy import func
from sqlmodel import Session, select

from knowledge.gardener import _slugify
from knowledge.models import Gap, NoteLink

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed gap-lifecycle errors
#
# Each carries the HTTP ``status_code`` its router endpoint should surface, so
# ``knowledge.router._map_gap_error`` maps by class instead of matching error
# substrings. All subclass ``ValueError`` so existing callers that ``except
# ValueError`` (the MCP tools return ``str(exc)``) keep working unchanged.
# ---------------------------------------------------------------------------


class GapError(ValueError):
    """Base for gap-lifecycle errors. Maps to HTTP 400 unless overridden."""

    status_code = 400


class GapNotFoundError(GapError):
    """No gap with the given id (or it is soft-deleted on a write path)."""

    status_code = 404


class GapWrongStateError(GapError):
    """The gap is not in the state the requested transition requires."""

    status_code = 409


class GapNotDeletedError(GapError):
    """``undelete`` was called on a gap that is not soft-deleted."""

    status_code = 409


def split_csv(value: str | None) -> list[str] | None:
    """Split a comma-separated query/tool param into a list, stripping
    whitespace and dropping empty segments. Returns None when input is None
    or all-empty so callers can pass it straight into optional filter kwargs.
    """
    if value is None:
        return None
    parts = [s.strip() for s in value.split(",") if s.strip()]
    return parts or None


# Terminal states for the audit queue. A gap in one of these states had
# its lifecycle ended (research auto-commits, classifier parks,
# reject_gap rejects). The audit queue surfaces
# them so a human can spot-check the automation's call.
_TERMINAL_GAP_STATES = ("committed", "rejected", "parked")


def _gap_to_dict(gap: Gap, session: Session | None = None) -> dict:
    """Serialize a Gap row to the dict shape returned by review-queue endpoints.

    Shared by the pending and audit list modes. Includes `state`,
    `resolved_at`, and `human_verified` so the audit UI can render
    timestamps and verification status without a second round-trip.

    When ``session`` is provided, the dict is enriched with fields the
    /private/review audit UI needs to evaluate a gap in-place without a
    second per-row fetch:

    * ``referenced_by_count`` — count of ``NoteLink`` rows pointing at
      this gap's term (slugified), so the UI can show "5 notes link to
      this term".
    * ``research_attempts`` / ``answer`` — already on the model; surfaced
      so the audit UI can show "researched 3 times, no answer captured".
    """
    payload = {
        "id": gap.id,
        "term": gap.term,
        "context": gap.context,
        "gap_class": gap.gap_class,
        "state": gap.state,
        "created_at": gap.created_at,
        "resolved_at": gap.resolved_at,
        "human_verified": gap.human_verified,
        "research_attempts": gap.research_attempts,
        "answer": gap.answer,
        "deleted_at": gap.deleted_at,
    }
    if session is not None:
        # NoteLink.target_id stores the raw wikilink text (case-preserved),
        # so match against both the canonical term AND its slugified form
        # to catch e.g. ``[[Steve Krug]]`` vs ``[[steve-krug]]`` linking
        # at the same conceptual target.
        slug = _slugify(gap.term)
        count = session.execute(
            select(func.count())
            .select_from(NoteLink)
            .where(NoteLink.kind == "link")
            .where(NoteLink.target_id.in_((gap.term, slug)))
        ).scalar_one()
        payload["referenced_by_count"] = int(count or 0)
    return payload


def list_gaps_for_review(
    session: Session,
    *,
    mode: str = "pending",
    limit: int = 50,
) -> list[dict]:
    """Return gaps for the private review page, filtered by ``mode``.

    ``mode='pending'`` — gaps awaiting user attention (``state='in_review'``,
    ``human_verified IS FALSE``). Oldest first.
    This is the same queue that backs the original ``list_review_queue``
    helper plus the ``human_verified`` filter so verified gaps drop out of
    the queue immediately after a ``/verify`` call.

    ``mode='audit'`` — terminal gaps (``committed|rejected|parked``) where
    ``human_verified IS FALSE``, most-recently-resolved first. NULL
    ``resolved_at`` sorts last (preserves the older behaviour where a
    terminal gap with no resolved_at was a corner-case row, not the
    first thing surfaced to the user).

    Both modes filter out soft-deleted rows (``deleted_at IS NULL``).

    Raises:
        ValueError: if ``mode`` is not one of ``pending``/``audit``.
    """
    if mode == "pending":
        stmt = (
            select(Gap)
            .where(Gap.state == "in_review")
            .where(Gap.gap_class.in_(("internal", "hybrid", "external")))
            .where(Gap.human_verified.is_(False))
            .where(Gap.deleted_at.is_(None))
            .order_by(Gap.created_at.asc(), Gap.id.asc())
            .limit(limit)
        )
    elif mode == "audit":
        stmt = (
            select(Gap)
            .where(Gap.state.in_(_TERMINAL_GAP_STATES))
            .where(Gap.human_verified.is_(False))
            .where(Gap.deleted_at.is_(None))
            .order_by(Gap.resolved_at.desc().nulls_last(), Gap.id.desc())
            .limit(limit)
        )
    else:
        raise GapError(f"unknown review-queue mode: {mode!r}")

    rows = session.execute(stmt).scalars().all()
    return [_gap_to_dict(gap, session=session) for gap in rows]


def list_review_queue(session: Session) -> list[dict]:
    """Return user-actionable gaps awaiting attention (internal and hybrid
    gaps await an answer from Joe, external gaps are drained directly by the
    research routine with no approval step), oldest first.

    Thin wrapper around :func:`list_gaps_for_review` for backward
    compatibility — preserved for the small number of in-tree callers
    (gap_lifecycle_test, gap_end_to_end_test, router test mocks) that
    expect the original signature. New code should call
    :func:`list_gaps_for_review` directly.
    """
    return list_gaps_for_review(session, mode="pending", limit=50)


def _get_gap_or_raise(session: Session, gap_id: int) -> Gap:
    """Load a Gap by id or raise :class:`GapNotFoundError`. Shared by
    reject/verify/reopen.

    The router maps :class:`GapNotFoundError` to HTTP 404. Soft-deleted gaps
    (``deleted_at IS NOT NULL``) are treated as not-found so write paths
    can't mutate them; use :func:`undelete_gap` to restore first.
    """
    gap = session.get(Gap, gap_id)
    if gap is None or gap.deleted_at is not None:
        raise GapNotFoundError(f"Gap not found: id={gap_id}")
    return gap


def reject_gap(session: Session, gap_id: int) -> dict:
    """Reject a pending gap: in_review → rejected (pure DB).

    Sets ``human_verified=True`` because the user explicitly took an
    action on the gap — rejection is a verification just like answering.

    Raises:
        ValueError: if ``gap_id`` is unknown or the gap is not in
            ``state='in_review'``.
    """
    gap = _get_gap_or_raise(session, gap_id)
    if gap.state != "in_review":
        raise GapWrongStateError(
            f"Gap id={gap_id} is in state={gap.state!r}, expected 'in_review'"
        )

    gap.state = "rejected"
    gap.resolved_at = datetime.now(timezone.utc)
    gap.human_verified = True
    session.commit()
    session.refresh(gap)
    logger.info("gaps.reject_gap: rejected gap_id=%d term=%r", gap_id, gap.term)
    return _gap_to_dict(gap, session=session)


def verify_gap(session: Session, gap_id: int) -> dict:
    """Mark ``human_verified=True`` on a gap. Works on any state.

    Pure acknowledgement — does not change ``state``, does not touch any
    file. From the pending queue: drops the gap out of the pending filter
    (which requires ``human_verified IS FALSE``) but keeps it in
    ``state='in_review'``. From the audit queue: same effect, just starting
    from a terminal state.

    Raises:
        ValueError: if ``gap_id`` is unknown.
    """
    gap = _get_gap_or_raise(session, gap_id)
    gap.human_verified = True
    session.commit()
    session.refresh(gap)
    logger.info("gaps.verify_gap: verified gap_id=%d state=%s", gap_id, gap.state)
    return _gap_to_dict(gap, session=session)


def reopen_gap(session: Session, gap_id: int) -> dict:
    """Reopen a terminal gap: committed|rejected|parked → in_review.

    Clears ``resolved_at`` and resets ``human_verified=False`` so the
    gap re-enters the pending queue for a fresh human decision (pure DB).

    Raises:
        ValueError: if ``gap_id`` is unknown or the gap is not in a
            terminal state.
    """
    gap = _get_gap_or_raise(session, gap_id)
    if gap.state not in _TERMINAL_GAP_STATES:
        raise GapWrongStateError(
            f"Gap id={gap_id} is in state={gap.state!r}, expected one of "
            f"{list(_TERMINAL_GAP_STATES)}"
        )

    gap.state = "in_review"
    gap.resolved_at = None
    gap.human_verified = False
    session.commit()
    session.refresh(gap)
    logger.info("gaps.reopen_gap: reopened gap_id=%d term=%r", gap_id, gap.term)
    return _gap_to_dict(gap, session=session)


def delete_gap(session: Session, gap_id: int) -> dict:
    """Soft-delete a gap. Sets ``deleted_at`` (pure DB).

    Idempotent on already-deleted rows — calling delete twice is a no-op
    that returns the same payload.

    Raises:
        ValueError: if ``gap_id`` is unknown. Soft-deleted gaps return
            the same 404 path via :func:`_get_gap_or_raise` UNLESS this
            function's idempotent short-circuit catches it first.
    """
    # Bypass _get_gap_or_raise's deleted-as-404 to make delete idempotent.
    gap = session.get(Gap, gap_id)
    if gap is None:
        raise GapNotFoundError(f"Gap not found: id={gap_id}")
    if gap.deleted_at is not None:
        return _gap_to_dict(gap, session=session)

    gap.deleted_at = datetime.now(timezone.utc)
    session.add(gap)
    session.commit()
    session.refresh(gap)
    logger.info("gaps.delete_gap: soft-deleted gap_id=%d term=%r", gap_id, gap.term)
    return _gap_to_dict(gap, session=session)


def undelete_gap(session: Session, gap_id: int) -> dict:
    """Undo soft-delete: clear ``deleted_at`` (pure DB). The gap re-enters
    queries; ``discover_gaps`` resurrects it on its next cycle if the source
    wikilinks still reference the term.

    Raises:
        ValueError: if ``gap_id`` is unknown, or if the gap is not
            currently soft-deleted (calling undelete on a live row is
            almost certainly a UI bug worth surfacing).
    """
    # Bypass _get_gap_or_raise's deleted-as-404 so we can find the row.
    gap = session.get(Gap, gap_id)
    if gap is None:
        raise GapNotFoundError(f"Gap not found: id={gap_id}")
    if gap.deleted_at is None:
        raise GapNotDeletedError(f"Gap id={gap_id} is not deleted")

    gap.deleted_at = None
    session.add(gap)
    session.commit()
    session.refresh(gap)
    logger.info("gaps.undelete_gap: restored gap_id=%d term=%r", gap_id, gap.term)
    return _gap_to_dict(gap, session=session)
