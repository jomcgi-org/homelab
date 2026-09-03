"""Note review-queue helpers: list, set/verify/reset visibility.

Mirrors :mod:`knowledge.gaps` for the note side of the private-review-page
feature. The four functions in this module back the HTTP routes added in
:mod:`knowledge.router`:

    list_notes_for_review   → pending / audit queue
    set_note_visibility     → set the visibility column + flip the verified flag
    verify_note_visibility  → flip the verified flag
    reset_note_visibility   → clear the visibility column + verified flag

All four take an open :class:`Session`; the caller owns the session
lifecycle, same convention as ``gaps.py`` and ``store.py``. Everything is
Postgres now (ADR 006, Obsidian decommissioned): ``knowledge.notes.content``
is the authoritative body, so listed notes carry a short snippet read from
that column rather than from disk.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

import yaml
from sqlmodel import Session, select

from knowledge import frontmatter
from knowledge.models import Note

logger = logging.getLogger(__name__)

# Snippet cap for the review-queue list calls. Was 200 chars when the
# review page first shipped — too thin to evaluate a card in audit mode.
# Bumped to 100 lines (capped by a byte ceiling so a single huge line
# can't blow the payload) so the audit UI can actually read what's
# inside without opening each row.
_SNIPPET_MAX_LINES = 100
_SNIPPET_MAX_BYTES = 8192


def _get_note_or_raise(session: Session, note_id: str) -> Note:
    """Load a Note by stable string ``note_id`` or raise ValueError.

    Mirrors :func:`knowledge.gaps._get_gap_or_raise` — the router layer
    maps ``"Note not found"`` substrings to HTTP 404. Soft-deleted notes
    (``deleted_at IS NOT NULL``) are treated as not-found so write paths
    can't mutate them; use :func:`undelete_note` to restore first.
    """
    note = session.exec(
        select(Note).where(Note.note_id == note_id).where(Note.deleted_at.is_(None))
    ).one_or_none()
    if note is None:
        raise ValueError(f"Note not found: note_id={note_id!r}")
    return note


def resolve_note_body(content: str | None) -> str | None:
    """Return a note's markdown body. Postgres ``content`` is authoritative.

    ADR 006: ``knowledge.notes.content`` holds the body (frontmatter and the
    generated ``## Links`` section already stripped at index time). The
    historical on-disk vault fallback is gone now that Obsidian is fully
    decommissioned and ``/vault`` no longer exists.
    """
    return content


def _read_note_snippet(note: Note) -> str:
    """Return the first ~100 lines of ``note``'s body, capped at 8 KiB.

    Reads the authoritative Postgres ``content`` (ADR 006). Missing bodies
    return an empty string — review-queue listing is best-effort metadata and
    must never break on a partial row. The line+byte cap is sized to give the
    audit UI enough context to evaluate a note in-place without surfacing the
    entire body.
    """
    body = resolve_note_body(note.content)
    if body is None:
        logger.warning(
            "notes._read_note_snippet: failed to read %s for note_id=%r",
            note.path,
            note.note_id,
        )
        return ""
    head = "\n".join(body.lstrip().splitlines()[:_SNIPPET_MAX_LINES])
    # Byte cap as a backstop: a single very long line shouldn't blow
    # the payload past what the review UI can render comfortably.
    encoded = head.encode("utf-8")
    if len(encoded) > _SNIPPET_MAX_BYTES:
        head = encoded[:_SNIPPET_MAX_BYTES].decode("utf-8", errors="ignore")
    return head


def _note_to_review_dict(note: Note) -> dict:
    """Serialize a Note row to the dict shape returned by review-queue endpoints.

    Emits a ~100-line snippet (was 200 chars before the audit-UI redesign)
    plus ``tags`` / ``type`` / ``source`` / ``deleted_at`` so the audit
    page can evaluate a card without a second round-trip per row. The
    Note model has no ``tier`` column today, so ``tier`` is omitted
    entirely; if/when one is added, surface it here.
    """
    updated_at = note.updated_at
    return {
        "id": note.note_id,
        "title": note.title,
        "snippet": _read_note_snippet(note),
        "visibility": note.visibility,
        "visibility_verified": note.visibility_verified,
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
        "tags": list(note.tags or []),
        "type": note.type,
        "source": note.source,
        "deleted_at": (
            note.deleted_at.isoformat() if note.deleted_at is not None else None
        ),
    }


def _serialize_frontmatter(parsed: frontmatter.ParsedFrontmatter, body: str) -> str:
    """Re-serialize a parsed frontmatter + body back to file content.

    Mirrors the dict-building pattern in :func:`knowledge.router.edit_note`
    so any change to frontmatter ordering / encoding stays in one shape.
    Promoted keys land first in their declared order; ``parsed.extra``
    appends afterward to preserve any non-promoted user fields.
    """
    fm_dict: dict = {}
    if parsed.note_id is not None:
        fm_dict["id"] = parsed.note_id
    if parsed.title is not None:
        fm_dict["title"] = parsed.title
    if parsed.type is not None:
        fm_dict["type"] = parsed.type
    if parsed.status is not None:
        fm_dict["status"] = parsed.status
    if parsed.visibility is not None:
        fm_dict["visibility"] = parsed.visibility
    if parsed.source is not None:
        fm_dict["source"] = parsed.source
    if parsed.scope is not None:
        fm_dict["scope"] = parsed.scope
    if parsed.verification_state is not None:
        fm_dict["verification_state"] = parsed.verification_state
    if parsed.confidence is not None:
        fm_dict["confidence"] = parsed.confidence
    if parsed.valid_from is not None:
        fm_dict["valid_from"] = parsed.valid_from.isoformat()
    if parsed.valid_until is not None:
        fm_dict["valid_until"] = parsed.valid_until.isoformat()
    if parsed.observed_at is not None:
        fm_dict["observed_at"] = parsed.observed_at.isoformat()
    if parsed.tags:
        fm_dict["tags"] = parsed.tags
    if parsed.aliases:
        fm_dict["aliases"] = parsed.aliases
    if parsed.edges:
        fm_dict["edges"] = parsed.edges
    if parsed.created is not None:
        fm_dict["created"] = parsed.created.isoformat()
    if parsed.updated is not None:
        fm_dict["updated"] = parsed.updated.isoformat()
    fm_dict.update(parsed.extra)

    fm_str = yaml.dump(fm_dict, default_flow_style=False, sort_keys=False)
    return f"---\n{fm_str}---\n\n{body}\n"


def set_note_visibility(
    session: Session,
    note_id: str,
    visibility: str,
) -> Note:
    """Set ``note.visibility`` (public|private) and mark verified.

    Updates the column and flips ``visibility_verified=True`` because the
    caller (a human) explicitly took an action — same convention as
    ``reject_gap`` / ``answer_gap``. Visibility lives only in the column
    now (ADR 006): ``content`` carries no frontmatter, so there is nothing
    to rewrite on disk.

    Raises:
        ValueError: if ``visibility`` is not exactly ``"public"`` or
            ``"private"``, or if ``note_id`` is unknown.
    """
    if visibility not in ("public", "private"):
        raise ValueError(
            f"visibility must be 'public' or 'private', got {visibility!r}"
        )
    note = _get_note_or_raise(session, note_id)
    # ``Visibility`` is ``Literal["public", "private"]``; the validation
    # above guarantees ``visibility`` matches one of those two strings.
    note.visibility = visibility  # type: ignore[assignment]
    note.visibility_verified = True
    session.add(note)
    session.commit()
    session.refresh(note)
    logger.info(
        "notes.set_note_visibility: note_id=%r visibility=%s", note_id, visibility
    )
    return note


def verify_note_visibility(session: Session, note_id: str) -> Note:
    """Mark ``visibility_verified=True``. Requires visibility already set.

    Returns 409-equivalent ValueError ("visibility is unset") when called
    on a note where ``visibility IS NULL`` — verification only makes
    sense once a public/private decision exists. No frontmatter change.

    Raises:
        ValueError: if ``note_id`` is unknown, or if ``note.visibility``
            is ``None``.
    """
    note = _get_note_or_raise(session, note_id)
    if note.visibility is None:
        raise ValueError(f"cannot verify note_id={note_id!r}: visibility is unset")
    note.visibility_verified = True
    session.add(note)
    session.commit()
    session.refresh(note)
    logger.info(
        "notes.verify_note_visibility: note_id=%r visibility=%s",
        note_id,
        note.visibility,
    )
    return note


def reset_note_visibility(
    session: Session,
    note_id: str,
) -> Note:
    """Clear ``note.visibility`` and ``visibility_verified``.

    Sends the note back to the pending review queue (``visibility IS
    NULL``) by zeroing the DB flags. The audit-mode "no, this decision was
    wrong" path. Visibility lives only in the column now (ADR 006).

    Raises:
        ValueError: if ``note_id`` is unknown.
    """
    note = _get_note_or_raise(session, note_id)
    note.visibility = None
    note.visibility_verified = False
    session.add(note)
    session.commit()
    session.refresh(note)
    logger.info("notes.reset_note_visibility: note_id=%r", note_id)
    return note


def list_notes_for_review(
    session: Session,
    *,
    mode: Literal["pending", "audit"] = "pending",
    limit: int = 50,
) -> list[dict]:
    """Return notes for the private review page, filtered by ``mode``.

    ``mode='pending'`` — notes whose ``visibility IS NULL`` (never
    classified), oldest-created first. Drain-to-zero queue.

    ``mode='audit'`` — notes with a visibility set but
    ``visibility_verified IS FALSE`` (automation classified, human
    hasn't confirmed). Most-recently-updated first; NULL ``updated_at``
    sorts last.
    """
    if mode == "pending":
        stmt = (
            select(Note)
            .where(Note.visibility.is_(None))
            .where(Note.deleted_at.is_(None))
            .order_by(Note.created_at.asc(), Note.id.asc())
            .limit(limit)
        )
    else:  # "audit" — Literal type guarantees no other value reaches here
        stmt = (
            select(Note)
            .where(Note.visibility.is_not(None))
            .where(Note.visibility_verified.is_(False))
            .where(Note.deleted_at.is_(None))
            .order_by(Note.updated_at.desc().nulls_last(), Note.id.desc())
            .limit(limit)
        )
    rows = session.execute(stmt).scalars().all()
    return [_note_to_review_dict(note) for note in rows]


def delete_note(session: Session, note_id: str) -> Note:
    """Soft-delete a note by stamping ``deleted_at``.

    Sets ``deleted_at = now()`` so the row drops out of every user-facing
    read path (review queue, graph, search, get-by-id). ``pre_delete_path``
    captures the current ``path`` so :func:`undelete_note` is symmetric.
    Bodies are authoritative in Postgres (ADR 006); there is no on-disk file
    to move now that the vault is gone.

    Raises:
        ValueError: if ``note_id`` is unknown OR already deleted (caller
            should call ``undelete_note`` instead of double-deleting).
    """
    note = _get_note_or_raise(session, note_id)
    note.pre_delete_path = note.path
    note.deleted_at = datetime.now(timezone.utc)
    session.add(note)
    session.commit()
    session.refresh(note)
    logger.info(
        "notes.delete_note: soft-deleted note_id=%r path=%s",
        note_id,
        note.path,
    )
    return note


def undelete_note(session: Session, note_id: str) -> Note:
    """Undo a soft-delete by clearing ``deleted_at``.

    The row reappears in every read path. Bodies are authoritative in
    Postgres (ADR 006); there is no file to move back now that the vault
    is gone, so this is a pure DB flag clear.

    Raises:
        ValueError: if ``note_id`` is unknown, or if the row is not in
            soft-deleted state. The message starts with ``"Note not
            found"`` so :func:`router._map_note_error` maps to 404.
    """
    # Query specifically for soft-deleted rows — is_not(None) satisfies the
    # deleted_at filter contract while only returning rows that are deleted.
    note = session.exec(
        select(Note).where(Note.note_id == note_id, Note.deleted_at.is_not(None))
    ).one_or_none()
    if note is None:
        raise ValueError(f"Note not found: note_id={note_id!r}")

    note.deleted_at = None
    note.pre_delete_path = None
    session.add(note)
    session.commit()
    session.refresh(note)
    logger.info("notes.undelete_note: restored note_id=%r path=%s", note_id, note.path)
    return note
