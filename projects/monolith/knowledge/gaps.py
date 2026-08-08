"""Gap discovery, review, and answer capture.

A gap is an unresolved body-text wikilink. Discovery writes unresolved terms
to Postgres, remote routines classify them through MCP, and review endpoints
capture or audit answers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from sqlalchemy import func
from sqlmodel import Session, select

from knowledge.gardener import _slugify
from knowledge.models import Gap, Note, NoteLink

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


class GapAnswerInvalidError(GapError):
    """The supplied answer text is invalid (e.g. a frontmatter terminator)."""

    status_code = 400


GAPS_PIPELINE_VERSION = "gaps@v1"
_VALID_GAP_CLASSES = frozenset({"external", "internal", "hybrid", "parked"})


def split_csv(value: str | None) -> list[str] | None:
    """Split a comma-separated query/tool param into a list, stripping
    whitespace and dropping empty segments. Returns None when input is None
    or all-empty so callers can pass it straight into optional filter kwargs.
    """
    if value is None:
        return None
    parts = [s.strip() for s in value.split(",") if s.strip()]
    return parts or None


# Non-terminal gap states the commit hook is allowed to close. A gap in one
# of these states is still "open" (awaiting research or a user answer); once
# an atom defining its term lands, the gap is resolved. Terminal states
# (committed/rejected/parked) are left untouched.
_OPEN_GAP_STATES = (
    "discovered",
    "classified",
    "in_review",
    "researching",
    "researched",
)

# The only gap_class values that may legally accompany state='committed' under
# the Postgres CHECK ``gaps_state_class_combo``. Gaps with gap_class NULL or
# 'parked' MUST NOT be committed: SQLite test fixtures do not enforce the
# CHECK, so an unguarded commit passes CI and 500s in prod.
_COMMITTABLE_CLASSES = ("external", "internal", "hybrid")


def resolve_gaps_for_note(
    session: Session,
    *,
    note_id: str,
    title: str,
    aliases: list[str] | None,
) -> list[int]:
    """Commit open gaps whose term now resolves to this note.

    Called after a note is indexed (create_atom / gardener / research routine).
    Matches ``gap.term`` (slug-normalized via the same ``_slugify`` used
    elsewhere in this module) against the note's ``note_id``, slugified
    ``title``, and slugified ``aliases``. Only commits gaps whose ``gap_class``
    is in the CHECK-combo legal set (external/internal/hybrid); gaps with
    NULL or 'parked' class are left open because committing them violates
    ``gaps_state_class_combo`` on Postgres (which SQLite does not catch).

    On match, sets ``state='committed'``, ``note_id=<this note>``,
    ``resolved_at=now(utc)``, ``human_verified=False``. Commits the session
    if any gap changed. Returns the list of committed gap ids.
    """
    candidates = {note_id, _slugify(title)}
    for alias in aliases or []:
        candidates.add(_slugify(alias))

    rows = (
        session.execute(
            select(Gap).where(
                Gap.deleted_at.is_(None),
                Gap.state.in_(_OPEN_GAP_STATES),
                Gap.gap_class.in_(_COMMITTABLE_CLASSES),
            )
        )
        .scalars()
        .all()
    )

    committed: list[int] = []
    now = datetime.now(timezone.utc)
    for gap in rows:
        if _slugify(gap.term) in candidates:
            gap.state = "committed"
            gap.note_id = note_id
            gap.resolved_at = now
            gap.human_verified = False
            committed.append(gap.id)

    if committed:
        session.commit()
        logger.info(
            "gaps.resolve_gaps_for_note: committed %d gap(s) for note_id=%s: %s",
            len(committed),
            note_id,
            committed,
        )
    return committed


def discover_gaps(session: Session) -> int:
    """Scan note_links for unresolved wikilinks and insert Gap rows (fileless).

    For each unresolved wikilink term (a ``kind='link'`` target that does not
    resolve to any existing ``note_id`` or slugified alias), insert a
    ``Gap(state='discovered', gap_class=None)`` row unless a gap already
    exists for that term/slug. Pure Postgres: no vault filesystem, no stub
    files. The claude.ai classify routine (``set_gap_class``) and research
    routine drain the rows from there.

    Healing semantics preserved from the vault era (both pure-DB, no files):
        * A legacy live Gap row whose ``note_id`` is still NULL is backfilled
          with ``note_id = slug(term)``.
        * A soft-deleted Gap row whose term is referenced again is resurrected
          (``deleted_at`` cleared) rather than re-inserted, which
          ``UNIQUE(note_id)`` would reject.

    Returns the count of Gap rows newly inserted or resurrected this cycle.
    Idempotent: a subsequent run with no new unresolved links returns 0.
    """
    # Collect existing note_ids once so the unresolved filter is a set
    # membership check (avoids a correlated subquery per row). Includes
    # slugified frontmatter aliases so wikilinks pointing at a canonical
    # atom under one of its aliases (e.g. `[[Bayes' Theorem]]` slugifies
    # to `bayes-theorem`, but the canonical atom may live at a different
    # slug with "Bayes' Theorem" in `aliases:`) don't get queued as
    # false-positive gaps. Mirrors the gardener atomizer's alias-preserving
    # contract — wherever the gardener writes aliases, the gap-detector
    # consults them.
    # Exclude type='gap' Notes: stubs were placeholders for unresolved
    # wikilinks, not resolved targets. Including them would shadow the slug
    # and hide the gap.
    existing_note_ids: set[str] = set()
    for note_id, aliases in session.execute(
        select(Note.note_id, Note.aliases).where(Note.type != "gap")
    ).all():
        if note_id:
            existing_note_ids.add(note_id)
        for alias in aliases or []:
            existing_note_ids.add(_slugify(alias))

    # All body-wikilink rows. Frontmatter edges (kind='edge') are not
    # treated as gaps — those are typed assertions, not unresolved
    # references that a human would be expected to answer.
    link_rows = session.execute(
        select(
            NoteLink.target_id,
            Note.title,
        )
        .join(Note, Note.id == NoteLink.src_note_fk)
        .where(NoteLink.kind == "link")
    ).all()

    # Phase 1: collect the first-seen context per unresolved term.
    #
    # ``NoteLink.target_id`` is the raw wikilink text (``Steve Krug``),
    # while ``existing_note_ids`` is a set of slugs (``steve-krug``) plus
    # slugified aliases. Slugify the target before the membership check so a
    # wikilink to an existing note resolves correctly regardless of casing,
    # spaces, or punctuation. First-writer wins for context.
    contexts: dict[str, str] = {}
    for row in link_rows:
        target_id = row.target_id
        if _slugify(target_id) in existing_note_ids:
            continue
        contexts.setdefault(target_id, row.title or "")

    # Phase 2: fold by slug. Two terms slugging to the same note_id collapse
    # into one Gap row. Sort terms so the canonical-term-per-slug is
    # reproducible across runs (otherwise the dict-iteration order would
    # pick whichever term landed first).
    slug_canonical_term: dict[str, str] = {}
    slug_context: dict[str, str] = {}
    for term in sorted(contexts.keys()):
        slug = _slugify(term)
        if slug not in slug_canonical_term:
            slug_canonical_term[slug] = term
            slug_context[slug] = contexts[term]

    # Pre-load Gap rows by both note_id (slug identity) and term (for legacy
    # backfill of rows where note_id is still NULL and the UNIQUE(term)
    # dedupe check).
    all_gaps = (
        session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
    )
    existing_by_note_id: dict[str, Gap] = {g.note_id: g for g in all_gaps if g.note_id}
    existing_by_term: dict[str, Gap] = {g.term: g for g in all_gaps}

    # Pre-load soft-deleted Gap rows by note_id for resurrection. When a
    # wikilink still points to a previously soft-deleted term, we clear
    # deleted_at rather than inserting a duplicate — UNIQUE(note_id) prevents
    # re-insertion and resurrection is the semantically correct behaviour
    # (the term is live again).
    soft_deleted_gaps = (
        session.execute(select(Gap).where(Gap.deleted_at.is_not(None))).scalars().all()
    )
    soft_deleted_by_note_id: dict[str, Gap] = {
        g.note_id: g for g in soft_deleted_gaps if g.note_id
    }

    inserted = 0
    backfilled = 0

    for slug, canonical_term in slug_canonical_term.items():
        # A live gap already keyed by this slug: nothing to do.
        if existing_by_note_id.get(slug) is not None:
            continue

        # UNIQUE(term) dedupe: a live gap already exists for this term.
        # Backfill its note_id if it pre-dates the slug-identity extension;
        # otherwise leave it untouched (a second insert would collide).
        legacy = existing_by_term.get(canonical_term)
        if legacy is not None:
            if legacy.note_id is None:
                legacy.note_id = slug
                backfilled += 1
            continue

        # Resurrect a soft-deleted gap rather than re-inserting it.
        soft_del = soft_deleted_by_note_id.get(slug)
        if soft_del is not None:
            soft_del.deleted_at = None
            inserted += 1
            continue

        # SAVEPOINT per insert: a concurrent discoverer could insert the same
        # slug between SELECT and INSERT. Nesting the add lets that single row
        # fail without rolling back every gap this cycle. With UNIQUE(note_id)
        # / UNIQUE(term) this is the last line of defence: slug-folding above
        # already collapses the in-process collisions.
        with session.begin_nested():
            session.add(
                Gap(
                    term=canonical_term,
                    context=slug_context[slug],
                    note_id=slug,
                    pipeline_version=GAPS_PIPELINE_VERSION,
                    state="discovered",
                )
            )
        inserted += 1

    if inserted or backfilled:
        session.commit()
        logger.info(
            "gaps.discover_gaps: inserted=%d backfilled_note_id=%d",
            inserted,
            backfilled,
        )
    return inserted


# Terminal states for the audit queue. A gap in one of these states had
# its lifecycle ended by automation (answer_gap commits, research auto-
# commits, classifier parks, reject_gap rejects). The audit queue surfaces
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
    ``human_verified IS FALSE``). For ``gap_class='internal'`` / ``hybrid``
    the user is expected to answer via :func:`answer_gap`. Oldest first.
    This is the same queue that backs the original ``list_review_queue``
    helper plus the ``human_verified`` filter so verified gaps drop out of
    the queue immediately after a ``/verify`` or ``/answer`` call.

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
    ``state='in_review'`` so the user can still answer it later via
    :func:`answer_gap`. From the audit queue: same effect, just starting
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


def set_gap_class(session: Session, gap_id: int, gap_class: str) -> dict:
    """Classify a discovered gap fileless and transition its state legally.

    Drives the claude.ai classification routine: it reads discovered,
    NULL-class gaps, applies the privacy rubric, and writes the decision
    back through this function. Every transition keeps the (state, gap_class)
    pair legal under the Postgres ``gaps_state_class_combo`` CHECK, which
    SQLite test fixtures do not enforce, so this transition logic is the
    only guard against an illegal pair reaching prod.

    Transitions from ``state='discovered'``:
        * ``external`` -> leave ``state='discovered'`` (the research routine
          pulls discovered and in_review external gaps, there is no
          intermediate state).
        * ``internal`` / ``hybrid`` -> ``state='in_review'`` (ready for a
          user answer via :func:`answer_gap`).
        * ``parked`` -> ``state='parked'`` and ``resolved_at=now(utc)``
          (terminal SKIP category).

    Raises:
        ValueError: if ``gap_class`` is not one of external/internal/hybrid/
            parked, if ``gap_id`` is unknown, or if the gap is not in
            ``state='discovered'`` (a non-discovered gap is already
            classified or in flight).
    """
    if gap_class not in _VALID_GAP_CLASSES:
        raise GapError(f"invalid gap_class: {gap_class!r}")
    gap = _get_gap_or_raise(session, gap_id)
    if gap.state != "discovered":
        raise GapWrongStateError(
            f"Gap id={gap_id} is in state={gap.state!r}, expected 'discovered'"
        )

    gap.gap_class = gap_class
    if gap_class in ("internal", "hybrid"):
        gap.state = "in_review"
    elif gap_class == "parked":
        gap.state = "parked"
        gap.resolved_at = datetime.now(timezone.utc)
    # external: leave state='discovered' for the research routine to pull.
    session.commit()
    session.refresh(gap)
    logger.info(
        "gaps.set_gap_class: gap_id=%d class=%s state=%s",
        gap_id,
        gap_class,
        gap.state,
    )
    return _gap_to_dict(gap, session=session)


def _is_tombstone_answer(answer: str) -> bool:
    """Detect the user's 'Tombstone — ...' convention on a gap answer.

    Matches the leading marker in any of the user's actual forms:
    ``Tombstone `` (capital T then ASCII space), ``Tombstone—`` (em-dash
    directly), ``Tombstone -`` (ASCII hyphen). Case-sensitive on the
    capital T — lowercased ``tombstone`` could plausibly appear in
    legitimate answers about graveyards, soft-deletes, or tombstone
    fields in storage engines, none of which the user wants
    short-circuited.
    """
    stripped = (answer or "").lstrip()
    if not stripped.startswith("Tombstone"):
        return False
    after = stripped[len("Tombstone") :]
    # space, em-dash, ASCII hyphen, or end-of-string all count.
    return after[:1] in (" ", "—", "-", "")


def _load_gap_for_answer(session: Session, gap_id: int) -> Gap:
    """Load an answerable gap, enforcing the in_review precondition (sync).

    Split out of :func:`answer_gap` so no raw ``session.*`` call sits in the
    async body (semgrep ``no-sync-session-in-async-def``). A gap in
    ``state='in_review'`` always carries ``gap_class`` in
    (internal, hybrid, external) under the ``gaps_state_class_combo`` CHECK, so
    this guard is what keeps the later commit legal.

    Raises:
        ValueError: if ``gap_id`` is unknown or the gap is not in
            ``state='in_review'``.
    """
    gap = session.get(Gap, gap_id)
    if gap is None or gap.deleted_at is not None:
        raise GapNotFoundError(f"Gap not found: id={gap_id}")
    if gap.state != "in_review":
        raise GapWrongStateError(
            f"Gap id={gap_id} is in state={gap.state!r}, expected 'in_review'"
        )
    return gap


def _reject_via_tombstone(session: Session, gap_id: int, answer: str) -> dict:
    """Close a gap via the Tombstone convention: rejected, no atom (sync).

    Split out of :func:`answer_gap` to keep ``session.*`` mutations and the
    commit out of the async body. ``answer`` is accepted for call-site
    symmetry; the Tombstone branch deliberately records no answer body.
    """
    del answer  # Tombstone gaps store no answer body (behavior preserved).
    gap = session.get(Gap, gap_id)
    gap.state = "rejected"
    gap.human_verified = True
    gap.resolved_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(gap)
    logger.info("gaps.answer_gap: tombstoned gap_id=%d term=%r", gap_id, gap.term)
    return _gap_to_dict(gap, session=session)


def _finalize_answered_gap(
    session: Session, gap_id: int, answer: str, note_id: str
) -> dict:
    """Mark an answered gap committed against its new atom (sync).

    Split out of :func:`answer_gap` to keep ``session.*`` mutations and the
    commit out of the async body.
    """
    gap = session.get(Gap, gap_id)
    gap.answer = answer
    gap.state = "committed"
    gap.note_id = note_id
    gap.resolved_at = datetime.now(timezone.utc)
    # User-initiated answer is itself a verification - keeps the
    # /private/review audit queue's "human has looked at this" semantics
    # consistent with reject_gap and verify_gap.
    gap.human_verified = True
    session.commit()
    logger.info(
        "gaps.answer_gap: committed gap_id=%d as note_id=%s",
        gap_id,
        note_id,
    )
    return {
        "gap_id": gap_id,
        "note_id": note_id,
    }


async def answer_gap(
    session: Session,
    gap_id: int,
    answer: str,
) -> dict:
    """Commit a user answer fileless: emit a personal-tier atom, mark committed.

    Routes the answer through the shared fileless index helper
    (:func:`knowledge.mcp._index_atom`), the same core ``create_atom`` uses, so
    the atom lands straight in Postgres with ``source_tier: personal`` and
    ``visibility: private``. No filesystem, no reconciler.

    The ``Tombstone`` answer convention closes the gap as ``rejected`` with no
    atom created (see :func:`_is_tombstone_answer`).

    A gap in ``state='in_review'`` always carries ``gap_class`` in
    (internal, hybrid, external) under the ``gaps_state_class_combo`` Postgres
    CHECK, so committing it is legal; the in_review precondition (enforced in
    :func:`_load_gap_for_answer`) is the in-code guard that upholds that
    invariant.

    All database-session calls live in the sync helpers
    (:func:`_load_gap_for_answer`, :func:`_reject_via_tombstone`,
    :func:`_finalize_answered_gap`) so none sit in this async body, matching
    how ``create_atom`` passes semgrep ``no-sync-session-in-async-def``.

    Raises:
        ValueError: if ``gap_id`` is unknown or the gap is not in
            ``state='in_review'``.
    """
    gap = _load_gap_for_answer(session, gap_id)
    if "\n---\n" in f"\n{answer}\n":
        raise GapAnswerInvalidError(
            "answer may not contain a frontmatter terminator ('---' on its own line)"
        )

    if _is_tombstone_answer(answer):
        # User convention: "Tombstone - <reason>" means "this gap doesn't
        # deserve a content atom". Route to reject semantics: gap is closed,
        # NO atom created. Honors the marker text the user has been typing for
        # months (2026-05-28 vault audit found 14 zombie atoms produced by the
        # answer-and-then-create-atom path on Tombstone-prefixed answers).
        return _reject_via_tombstone(session, gap_id, answer)

    # Local import breaks the knowledge.mcp <-> knowledge.gaps cycle: mcp
    # imports answer_gap, so answer_gap reaches back for the shared index
    # helper only at call time. The new atom defaults to visibility private:
    # gap answers are typically Joe writing about his own context (people,
    # projects, personal decisions); the visibility-review queue flips the
    # minority that should be public. Public-default would be riskier because
    # the body is user-supplied free-form text. See profile.py's
    # ASYMMETRIC_ERROR_PREFERENCE: 'private' is the right error direction.
    from knowledge.mcp import _index_atom

    note_id = await _index_atom(
        session,
        title=gap.term,
        body=answer,
        type="atom",
        visibility="private",
        source_tier="personal",
    )
    return _finalize_answered_gap(session, gap_id, answer, note_id)


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
