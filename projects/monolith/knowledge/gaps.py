"""Gap discovery, classification, review queue, and answer capture.

A "gap" is an unresolved ``[[wikilink]]`` — a body-text reference whose
target note does not (yet) exist in the graph. The four functions in this
module together drive the gap lifecycle:

    discover_gaps  → ingest unresolved links into the gaps table
    classify_gaps  → route each gap to external/internal/hybrid/parked
    list_review_queue  → enumerate gaps awaiting a user answer
    answer_gap     → accept a user answer, emit a personal-tier atom

All four are pure-ish functions taking an open ``Session``; the caller
owns the session lifecycle. This matches the rest of the knowledge
module (``store.py``, ``gardener.py``).

Design notes:
    * ``classifier`` is an injected callable — the real Claude-backed
      classifier is wired in separately (Task 3). Leaving it ``None`` is
      a no-op: gaps stay at ``state='discovered'`` until a real classifier
      lands. The "privacy-conservative default" in the design doc is about
      classifier output under uncertainty — not the absence of a
      classifier. Local inference has zero privacy cost; only external
      research does.
    * Consolidation never mutates committed atoms — ``answer_gap``
      creates a brand-new file and leaves the rest of the vault alone.
    * We deliberately do *not* reconcile the new file into the DB here;
      the reconciler picks it up on its next tick, matching the existing
      ``create_note`` HTTP endpoint.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml
from sqlalchemy import func
from sqlmodel import Session, select

from knowledge.gap_stubs import RESEARCHING_DIR, write_stub
from knowledge.gap_unlinkify import is_discardable, unlinkify_if_changed
from knowledge.gardener import _slugify
from knowledge.models import Gap, Note, NoteLink

logger = logging.getLogger(__name__)

GAPS_PIPELINE_VERSION = "gaps@v1"


def split_csv(value: str | None) -> list[str] | None:
    """Split a comma-separated query/tool param into a list, stripping
    whitespace and dropping empty segments. Returns None when input is None
    or all-empty so callers can pass it straight into optional filter kwargs.
    """
    if value is None:
        return None
    parts = [s.strip() for s in value.split(",") if s.strip()]
    return parts or None


# Privacy-conservative fallback used only when a real classifier returns an
# invalid class — route uncertain output to the user (internal), not the web.
# This is NOT used when classifier is absent (that path is a no-op).
_DEFAULT_GAP_CLASS = "internal"

# Classes that are ready for user review after classification.
_USER_REVIEW_CLASSES = {"internal", "hybrid"}

# Valid classifier outputs — mirrors the CHECK constraint on gaps.gap_class.
_VALID_GAP_CLASSES = frozenset({"external", "internal", "hybrid", "parked"})


def _rewrite_sources(
    session: Session,
    vault_root: Path,
    slug: str,
    source_note_ids: list[str],
    *,
    dry_run: bool,
) -> int:
    """Rewrite ``[[X]]`` -> bare text in every source note whose body links to ``slug``.

    Looks up each source note's on-disk path via ``Note.path``, runs
    :func:`unlinkify_if_changed` against the body, and writes back unless
    ``dry_run`` is True. Returns the count of notes whose bodies would
    change (or did change). FileNotFound / OSError on any individual
    file is logged and skipped — the loop must not abort because one
    source note got renamed or moved.
    """
    rows = session.execute(
        select(Note.note_id, Note.path).where(Note.note_id.in_(source_note_ids))
    ).all()
    touched = 0
    for _note_id, rel_path in rows:
        abs_path = vault_root / rel_path
        try:
            body = abs_path.read_text()
        except (FileNotFoundError, OSError):
            logger.warning(
                "gaps._rewrite_sources: could not read %s for slug=%s",
                abs_path,
                slug,
            )
            continue
        new_body = unlinkify_if_changed(body, {slug})
        if new_body is None:
            continue
        touched += 1
        if not dry_run:
            try:
                abs_path.write_text(new_body)
            except OSError:
                logger.warning(
                    "gaps._rewrite_sources: write failed for %s slug=%s",
                    abs_path,
                    slug,
                )
                touched -= 1  # don't claim a write that didn't happen
    return touched


def discover_gaps(session: Session, vault_root: Path) -> int:
    """Scan note_links for unresolved wikilinks; insert Gap rows and write stubs.

    For each unresolved term:
        * Insert a Gap row if one doesn't already exist (UNIQUE on term).
        * Backfill ``note_id = slug(term)`` on existing rows that pre-date
          this extension.
        * Write a stub note at ``_researching/<slug>.md`` containing the
          accumulated ``referenced_by`` list. ``write_stub`` is idempotent
          so existing stubs (including classifier-edited ones) survive.

    Healing semantics:
        * Gap row exists but stub is missing → write the stub.
        * Stub exists but Gap row is missing → insert the Gap row.
        * Two terms that slug to the same note_id collapse into one Gap row;
          their ``referenced_by`` lists are unioned in the surviving stub.

    Returns the number of "new" items — the count of Gap rows newly inserted
    OR stub files newly written (either side indicates this cycle did work).
    Idempotent: a subsequent run with no changes returns 0.
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
    # Exclude type='gap' Notes: stubs are placeholders for unresolved
    # wikilinks, not resolved targets. Including them shadows the slug
    # in slug_refs, which prevents Phase A from ever seeing wikilinks
    # to discardable stubs — the bug that left ~600 discardable stubs
    # untouched cycle after cycle until this fix landed.
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
            NoteLink.src_note_fk,
            NoteLink.target_id,
            Note.title,
            Note.note_id,
        )
        .join(Note, Note.id == NoteLink.src_note_fk)
        .where(NoteLink.kind == "link")
    ).all()

    # Phase 1: accumulate per-term breadcrumbs. One term can be referenced
    # by many source notes; the stub's referenced_by reflects that.
    #
    # ``NoteLink.target_id`` is the raw wikilink text (``Steve Krug``),
    # while ``existing_note_ids`` is a set of slugs (``steve-krug``) plus
    # slugified aliases. Slugify the target before the membership check
    # so a wikilink to an existing note resolves correctly regardless of
    # casing, spaces, or punctuation. Without this, every ``[[Title Case
    # Term]]`` to a `_processed/<slug>.md` note creates a Gap row that
    # only Sonnet's filesystem inspection can later catch and discard --
    # one full Sonnet call per duplicate that should never have been
    # queued. ``referenced_by`` is still keyed by the raw target so the
    # downstream slug-fold (Phase 2) preserves the canonical wikilink
    # text for ``slug_canonical_term``.
    referenced_by: dict[str, set[str]] = {}
    contexts: dict[str, str] = {}
    for row in link_rows:
        target_id = row.target_id
        if _slugify(target_id) in existing_note_ids:
            continue
        referenced_by.setdefault(target_id, set()).add(row.note_id)
        # First-writer wins for context — legacy breadcrumb; the stub's
        # referenced_by is authoritative.
        contexts.setdefault(target_id, row.title or "")

    # Phase 2: fold by slug. Two terms slugging to the same note_id collapse
    # into one slug entry; their referenced_by sets are unioned. Sort terms
    # so the canonical-term-per-slug is reproducible across runs (otherwise
    # the dict-iteration order would pick whichever term landed first).
    slug_refs: dict[str, set[str]] = {}
    slug_canonical_term: dict[str, str] = {}
    slug_context: dict[str, str] = {}
    for term in sorted(referenced_by.keys()):
        slug = _slugify(term)
        slug_refs.setdefault(slug, set()).update(referenced_by[term])
        if slug not in slug_canonical_term:
            slug_canonical_term[slug] = term
            slug_context[slug] = contexts.get(term, "")

    # Pre-load Gap rows by both note_id (post-stub identity) and term (for
    # legacy backfill of rows where note_id is still NULL).
    all_gaps = (
        session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
    )
    existing_by_note_id: dict[str, Gap] = {g.note_id: g for g in all_gaps if g.note_id}
    existing_by_term: dict[str, Gap] = {g.term: g for g in all_gaps}

    stub_dir = vault_root / RESEARCHING_DIR
    # Canonical Zulu form (no microseconds, no offset) — matches the design
    # doc examples and the test fixture strings, keeps stub frontmatter
    # visually consistent across files.
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0
    stubs_written = 0
    backfilled = 0
    new_items = 0
    rewrite_enabled = os.environ.get(
        "KNOWLEDGE_GAPS_REWRITE_DISCARDABLE", ""
    ).lower() in {"1", "true", "yes"}
    rewrites_applied = 0
    rewrites_dryrun = 0

    for slug, refs in slug_refs.items():
        canonical_term = slug_canonical_term[slug]
        refs_sorted = sorted(refs)
        row_inserted = False

        # Phase A: when the stub for this slug has triaged: discardable, the
        # user has explicitly said "this isn't a gap worth tracking — strip
        # the wikilinks from source bodies and stop refreshing the stub."
        # Behind a feature flag while the behaviour bakes; default off is a
        # dry-run that just logs how many notes WOULD be rewritten.
        stub_path = stub_dir / f"{slug}.md"
        if is_discardable(stub_path) and refs_sorted:
            touched = _rewrite_sources(
                session,
                vault_root,
                slug,
                refs_sorted,
                dry_run=not rewrite_enabled,
            )
            if rewrite_enabled:
                rewrites_applied += touched
            else:
                rewrites_dryrun += touched
            continue  # Skip upsert + write_stub for discardable stubs.

        existing = existing_by_note_id.get(slug)
        if existing is None:
            legacy = existing_by_term.get(canonical_term)
            if legacy is not None and legacy.note_id is None:
                # Backfill: legacy row pre-dates the stub-notes extension.
                legacy.note_id = slug
                backfilled += 1
            else:
                # SAVEPOINT per insert: a concurrent discoverer could insert
                # the same slug between SELECT and INSERT. Nesting the add lets
                # that single row fail without rolling back every gap this
                # cycle. With Task 1's UNIQUE(note_id) in place this is the
                # last line of defence — slug-folding above already collapses
                # the in-process collisions.
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
                row_inserted = True

        # Stub write is unconditional — write_stub is idempotent. Track whether
        # a new stub was actually written so we can surface healing work.
        # ``stub_path`` was set above before the discardable short-circuit.
        stub_existed = stub_path.exists()
        write_stub(
            vault_root=vault_root,
            note_id=slug,
            title=slug,
            referenced_by=refs_sorted,
            discovered_at=now_iso,
        )
        stub_newly_written = not stub_existed
        if stub_newly_written:
            stubs_written += 1

        # Count one unit of "work done" per slug where EITHER a new row was
        # inserted OR a new stub was written. Without this OR-collapse, the
        # common case of a first-time discovery would double-count (row + stub)
        # against a single observable gap.
        if row_inserted or stub_newly_written:
            new_items += 1

    # Snapshot (gap, note_id) pairs BEFORE the commit. SQLAlchemy's
    # expire_on_commit=True default would otherwise expire every Gap
    # instance in `all_gaps`, turning the Phase B loop's note_id reads
    # into N round-trip SELECTs.
    gap_candidates = [(g, g.note_id) for g in all_gaps if g.note_id]

    if inserted or backfilled:
        session.commit()

    # Phase B: tombstone discardable gaps whose source links are gone.
    #
    # A Gap row whose note_id is NOT in this cycle's slug_refs has zero
    # inbound wikilinks (note_links was authoritative for slug_refs above).
    # When such a gap's stub is marked triaged: discardable, the user has
    # said "this concept is closed — clean it up." Delete the row and the
    # stub together. Stubs marked keep / unmarked are preserved: a missing
    # stub or a non-discardable marker means we never tombstone, so an
    # orphan gap row from a deleted source note doesn't get blown away
    # unintentionally — only user-triaged-discardable gaps reach this branch.
    present_slugs = set(slug_refs.keys())
    tombstoned = 0
    for gap, gap_note_id in gap_candidates:
        if gap_note_id in present_slugs:
            continue
        stub_for_gap = stub_dir / f"{gap_note_id}.md"
        if not is_discardable(stub_for_gap):
            continue
        session.delete(gap)
        try:
            stub_for_gap.unlink()
        except FileNotFoundError:
            pass  # idempotent — already gone
        tombstoned += 1

    if tombstoned:
        session.commit()

    if new_items or backfilled or rewrites_applied or rewrites_dryrun or tombstoned:
        logger.info(
            "gaps.discover_gaps: inserted=%d backfilled_note_id=%d "
            "stubs_written=%d rewrites_applied=%d rewrites_dryrun=%d "
            "tombstoned=%d",
            inserted,
            backfilled,
            stubs_written,
            rewrites_applied,
            rewrites_dryrun,
            tombstoned,
        )
    return new_items


def classify_gaps(
    session: Session,
    classifier: Callable[[str, str], str] | None = None,
) -> int:
    """Classify every ``state='discovered'`` gap.

    ``classifier`` receives ``(term, context)`` and returns one of
    ``external``, ``internal``, ``hybrid``, ``parked``. If the classifier
    returns an invalid value, falls back to ``internal``
    (privacy-conservative under classifier uncertainty).

    When ``classifier`` is ``None``, this is a no-op: gaps stay at
    ``state='discovered'`` because no classifier is wired in yet. A
    warning is logged when pending gaps exist so the absence is visible.
    The review queue only populates once a real classifier lands (Task 3).

    State transitions (real-classifier path):
        * ``internal`` / ``hybrid`` → ``in_review`` (ready for user)
        * ``external`` → ``classified`` (research pipeline deferred)
        * ``parked`` → ``classified`` (queryable, not budget-consuming)

    Returns the number of gaps classified. When ``classifier`` is ``None``,
    returns 0.
    """
    if classifier is None:
        pending = session.execute(
            select(func.count()).select_from(Gap).where(Gap.state == "discovered")
        ).scalar_one()
        if pending:
            logger.warning(
                "gaps.classify_gaps: %d gaps awaiting classification but no "
                "classifier is wired; leaving them at state='discovered'",
                pending,
            )
        return 0

    rows = (
        session.execute(
            select(Gap).where(Gap.deleted_at.is_(None), Gap.state == "discovered")
        )
        .scalars()
        .all()
    )

    classified = 0
    now = datetime.now(timezone.utc)
    for gap in rows:
        gap_class = classifier(gap.term, gap.context)
        if gap_class not in _VALID_GAP_CLASSES:
            logger.warning(
                "gaps.classify_gaps: classifier returned invalid class %r "
                "for gap id=%d; defaulting to internal (privacy-conservative)",
                gap_class,
                gap.id,
            )
            gap_class = _DEFAULT_GAP_CLASS

        gap.gap_class = gap_class
        gap.classified_at = now
        if gap_class in _USER_REVIEW_CLASSES:
            gap.state = "in_review"
        else:
            gap.state = "classified"
        classified += 1

    if classified:
        # TODO(task-3): chunk commits (e.g. every 25 gaps) once the real
        # model-backed classifier is wired in. A 100-gap batch × 20s/call holds a
        # 2000s transaction; risks idle_in_transaction_session_timeout and loses
        # progress on crash.
        session.commit()
        logger.info(
            "gaps.classify_gaps: classified %d gaps",
            classified,
        )
    return classified


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

    ``stub_body`` is NOT loaded here because the stub path is keyed by
    ``vault_root`` which lives at the router layer. Callers that want it
    (the review-queue endpoints) call ``_attach_stub_body`` after.
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


# Cap stub-body bytes returned per gap so the review-queue payload stays
# small even when classifier dumped a long research draft into the stub.
_STUB_BODY_MAX_BYTES = 4096


def _read_stub_body(vault_root: Path, gap: Gap) -> str | None:
    """Return the first ~4 KiB of the gap's `_researching/<slug>.md` stub.

    Returns ``None`` when the stub file doesn't exist (deleted by
    ``reject_gap`` / ``answer_gap``, or never written). Errors are
    swallowed and returned as ``None`` — review-queue listing is best-
    effort metadata and must never break on a partial vault.
    """
    slug = _slugify(gap.term)
    stub_path = vault_root / RESEARCHING_DIR / f"{slug}.md"
    try:
        if not stub_path.is_file():
            return None
        with stub_path.open("rb") as fh:
            raw = fh.read(_STUB_BODY_MAX_BYTES + 1)
    except OSError:
        logger.warning(
            "gaps._read_stub_body: failed to read %s for gap_id=%s",
            stub_path,
            gap.id,
        )
        return None
    if len(raw) > _STUB_BODY_MAX_BYTES:
        raw = raw[:_STUB_BODY_MAX_BYTES]
    # decode(errors="replace") cannot raise on any byte sequence; no
    # try/except needed (bare ``except Exception`` is also blocked by
    # the no-broad-except-swallow semgrep rule).
    return raw.decode("utf-8", errors="replace")


def list_gaps_for_review(
    session: Session,
    *,
    mode: str = "pending",
    limit: int = 50,
    vault_root: Path | None = None,
) -> list[dict]:
    """Return gaps for the private review page, filtered by ``mode``.

    ``mode='pending'`` — gaps awaiting a user answer (``state='in_review'``,
    ``gap_class IN ('internal','hybrid')``, ``human_verified IS FALSE``),
    oldest first. This is the same queue that backs the original
    ``list_review_queue`` helper plus the new ``human_verified`` filter
    so verified gaps drop out of the queue immediately after a ``/verify``
    or ``/answer`` call.

    ``mode='audit'`` — terminal gaps (``committed|rejected|parked``) where
    ``human_verified IS FALSE``, most-recently-resolved first. NULL
    ``resolved_at`` sorts last (preserves the older behaviour where a
    terminal gap with no resolved_at was a corner-case row, not the
    first thing surfaced to the user).

    Both modes filter out soft-deleted rows (``deleted_at IS NULL``).
    When ``vault_root`` is provided, each row's ``stub_body`` is read
    from ``_researching/<slug>.md`` so the audit UI has enough body
    text to evaluate the gap without a per-row round-trip.

    Raises:
        ValueError: if ``mode`` is not one of ``pending``/``audit``.
    """
    if mode == "pending":
        stmt = (
            select(Gap)
            .where(Gap.state == "in_review")
            .where(Gap.gap_class.in_(("internal", "hybrid")))
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
        raise ValueError(f"unknown review-queue mode: {mode!r}")

    rows = session.execute(stmt).scalars().all()
    payloads: list[dict] = []
    for gap in rows:
        payload = _gap_to_dict(gap, session=session)
        if vault_root is not None:
            payload["stub_body"] = _read_stub_body(vault_root, gap)
        payloads.append(payload)
    return payloads


def list_review_queue(session: Session) -> list[dict]:
    """Return internal/hybrid gaps awaiting a user answer, oldest first.

    Thin wrapper around :func:`list_gaps_for_review` for backward
    compatibility — preserved for the small number of in-tree callers
    (gap_lifecycle_test, gap_end_to_end_test, router test mocks) that
    expect the original signature. New code should call
    :func:`list_gaps_for_review` directly.
    """
    return list_gaps_for_review(session, mode="pending", limit=50)


def _get_gap_or_raise(session: Session, gap_id: int) -> Gap:
    """Load a Gap by id or raise ValueError. Shared by reject/verify/reopen.

    Mirrors :func:`answer_gap`'s error contract — the router layer maps
    ``"Gap not found"`` substrings to HTTP 404. Soft-deleted gaps
    (``deleted_at IS NOT NULL``) are treated as not-found so write paths
    can't mutate them; use :func:`undelete_gap` to restore first.
    """
    gap = session.get(Gap, gap_id)
    if gap is None or gap.deleted_at is not None:
        raise ValueError(f"Gap not found: id={gap_id}")
    return gap


def _remove_stub_if_present(vault_root: Path, gap: Gap, *, action: str) -> None:
    """Tombstone the ``_researching/<slug>.md`` stub for ``gap`` if it exists.

    Matches :func:`answer_gap`'s stub cleanup: the stub is keyed by the
    base slug of ``gap.term``, not by the (possibly suffixed) atom note_id.
    Missing stub is tolerated. ``action`` is a free-form verb that flows
    into the log line so different callers (``answer``/``reject``) are
    distinguishable in logs.
    """
    slug = _slugify(gap.term)
    stub_path = vault_root / RESEARCHING_DIR / f"{slug}.md"
    if stub_path.is_file():
        stub_path.unlink()
        logger.info(
            "gaps._remove_stub_if_present: removed stub %s for %s gap_id=%d",
            stub_path.relative_to(vault_root),
            action,
            gap.id,
        )


def reject_gap(
    session: Session,
    gap_id: int,
    vault_root: Path,
) -> dict:
    """Reject a pending gap: in_review → rejected, tombstone the stub.

    Sets ``human_verified=True`` because the user explicitly took an
    action on the gap — rejection is a verification just like answering.

    Raises:
        ValueError: if ``gap_id`` is unknown or the gap is not in
            ``state='in_review'``.
    """
    gap = _get_gap_or_raise(session, gap_id)
    if gap.state != "in_review":
        raise ValueError(
            f"Gap id={gap_id} is in state={gap.state!r}, expected 'in_review'"
        )

    gap.state = "rejected"
    gap.resolved_at = datetime.now(timezone.utc)
    gap.human_verified = True
    _remove_stub_if_present(vault_root, gap, action="rejected")
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
    gap re-enters the pending queue for a fresh human decision. Does not
    re-write the ``_researching`` stub — if the user wants to re-research,
    they can flip ``triaged: keep`` on any surviving stub or hand-create
    a new one. The pending UI doesn't need the stub.

    Raises:
        ValueError: if ``gap_id`` is unknown or the gap is not in a
            terminal state.
    """
    gap = _get_gap_or_raise(session, gap_id)
    if gap.state not in _TERMINAL_GAP_STATES:
        raise ValueError(
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


def answer_gap(
    session: Session,
    gap_id: int,
    answer: str,
    vault_root: Path,
) -> dict:
    """Commit a user answer: emit a personal-tier atom, mark gap committed.

    The new atom is written to ``<vault_root>/_processed/<slug>.md`` with
    frontmatter ``source_tier: personal`` so downstream consumers can
    distinguish user-authored atoms from gardener-derived ones. Filename
    collisions are resolved by appending ``-1``, ``-2``, etc. — same
    pattern as :func:`knowledge.router.create_note`.

    The new file is *not* reconciled into the DB here; the reconciler
    picks it up on its next tick. This matches ``create_note``'s contract.

    Raises:
        ValueError: if ``gap_id`` is unknown or the gap is not in
            ``state='in_review'``.
    """
    gap = session.get(Gap, gap_id)
    if gap is None or gap.deleted_at is not None:
        raise ValueError(f"Gap not found: id={gap_id}")
    if gap.state != "in_review":
        raise ValueError(
            f"Gap id={gap_id} is in state={gap.state!r}, expected 'in_review'"
        )
    if "\n---\n" in f"\n{answer}\n":
        raise ValueError(
            "answer may not contain a frontmatter terminator ('---' on its own line)"
        )

    processed_root = vault_root / "_processed"
    processed_root.mkdir(parents=True, exist_ok=True)

    slug = _slugify(gap.term)
    filename = f"{slug}.md"
    dest = processed_root / filename
    counter = 1
    while dest.exists():
        filename = f"{slug}-{counter}.md"
        dest = processed_root / filename
        counter += 1

    # The id in frontmatter must match the final filename stem so the
    # reconciler resolves the file to a stable note_id.
    note_id = filename[:-3]  # strip .md
    fm = {
        "id": note_id,
        "title": gap.term,
        "type": "atom",
        "source_tier": "personal",
    }
    fm_str = yaml.dump(fm, default_flow_style=False, sort_keys=False)
    file_content = f"---\n{fm_str}---\n\n{answer}\n"
    dest.write_text(file_content)

    # Remove the stub — its purpose ends once the atom exists. Missing
    # stub is tolerated (user may have hand-deleted it; the atom-at-
    # _processed/ write above is the actual source of truth now). Use
    # the base slug, NOT the collision-suffixed atom note_id — stubs are
    # always created at _researching/<base-slug>.md.
    stub_path = vault_root / RESEARCHING_DIR / f"{slug}.md"
    if stub_path.is_file():
        stub_path.unlink()
        logger.info(
            "gaps.answer_gap: removed stub %s for committed gap_id=%d",
            stub_path.relative_to(vault_root),
            gap_id,
        )

    gap.answer = answer
    gap.state = "committed"
    gap.resolved_at = datetime.now(timezone.utc)
    # User-initiated answer is itself a verification — keeps the
    # /private/review audit queue's "human has looked at this" semantics
    # consistent with reject_gap and verify_gap.
    gap.human_verified = True
    # TODO(task-3): make file-write + DB-commit transactional (e.g. write to
    # <dest>.tmp, commit DB, rename). Today a commit failure after write leaves
    # an orphan file that a retry resolves to <slug>-1.md.
    session.commit()

    relative_path = dest.relative_to(vault_root)
    logger.info(
        "gaps.answer_gap: committed gap_id=%d as note_id=%s path=%s",
        gap_id,
        note_id,
        relative_path,
    )
    return {
        "gap_id": gap_id,
        "path": str(relative_path),
        "note_id": note_id,
    }


def delete_gap(session: Session, gap_id: int, vault_root: Path) -> dict:
    """Soft-delete a gap. Sets ``deleted_at``; hard-deletes the stub file.

    Stubs at ``_researching/<slug>.md`` are regenerable from the gap's
    ``term`` (``discover_gaps`` will write a fresh one on its next tick
    after undelete, provided there's still at least one source wikilink
    pointing at the term). Hard-deleting the file now keeps the vault
    tidy without losing anything that can't be reconstructed.

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
        raise ValueError(f"Gap not found: id={gap_id}")
    if gap.deleted_at is not None:
        return _gap_to_dict(gap, session=session)

    gap.deleted_at = datetime.now(timezone.utc)
    _remove_stub_if_present(vault_root, gap, action="soft-deleted")
    session.add(gap)
    session.commit()
    session.refresh(gap)
    logger.info("gaps.delete_gap: soft-deleted gap_id=%d term=%r", gap_id, gap.term)
    return _gap_to_dict(gap, session=session)


def undelete_gap(session: Session, gap_id: int) -> dict:
    """Undo soft-delete: clear ``deleted_at``. The stub is regenerated
    lazily by the next ``discover_gaps`` cycle (or stays absent if the
    source wikilinks were rewritten/removed in the interim).

    Raises:
        ValueError: if ``gap_id`` is unknown, or if the gap is not
            currently soft-deleted (calling undelete on a live row is
            almost certainly a UI bug worth surfacing).
    """
    # Bypass _get_gap_or_raise's deleted-as-404 so we can find the row.
    gap = session.get(Gap, gap_id)
    if gap is None:
        raise ValueError(f"Gap not found: id={gap_id}")
    if gap.deleted_at is None:
        raise ValueError(f"Gap id={gap_id} is not deleted")

    gap.deleted_at = None
    session.add(gap)
    session.commit()
    session.refresh(gap)
    logger.info("gaps.undelete_gap: restored gap_id=%d term=%r", gap_id, gap.term)
    return _gap_to_dict(gap, session=session)
