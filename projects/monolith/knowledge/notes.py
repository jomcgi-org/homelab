"""Note review-queue helpers: list, set/verify/reset visibility.

Mirrors :mod:`knowledge.gaps` for the note side of the private-review-page
feature. The four functions in this module back the HTTP routes added in
:mod:`knowledge.router`:

    list_notes_for_review   → pending / audit queue
    set_note_visibility     → write frontmatter + flip the verified flag
    verify_note_visibility  → flip the verified flag (no frontmatter change)
    reset_note_visibility   → clear frontmatter visibility + verified flag

All four take an open :class:`Session`; the caller owns the session
lifecycle, same convention as ``gaps.py`` and ``store.py``. Notes that
are listed always include a short body snippet read from disk so the
review UI can show context without a second round-trip per row.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import yaml
from sqlmodel import Session, select

from knowledge import frontmatter
from knowledge.models import Note

logger = logging.getLogger(__name__)

# Length cap for the body snippet returned by review-queue list calls.
# Keeps the per-row payload small; the full body is loadable via
# GET /api/knowledge/notes/{note_id} when the user opens a row.
_SNIPPET_CHARS = 200


def _get_note_or_raise(session: Session, note_id: str) -> Note:
    """Load a Note by stable string ``note_id`` or raise ValueError.

    Mirrors :func:`knowledge.gaps._get_gap_or_raise` — the router layer
    maps ``"Note not found"`` substrings to HTTP 404.
    """
    note = session.exec(select(Note).where(Note.note_id == note_id)).one_or_none()
    if note is None:
        raise ValueError(f"Note not found: note_id={note_id!r}")
    return note


def _read_note_snippet(vault_root: Path, note: Note) -> str:
    """Return the first ``_SNIPPET_CHARS`` chars of ``note``'s body.

    Reads the vault file and strips frontmatter. Missing / unreadable
    files return an empty string — review-queue listing is best-effort
    metadata and must never break on a partial vault.
    """
    try:
        resolved = (vault_root / note.path).resolve()
        if not resolved.is_relative_to(vault_root) or not resolved.is_file():
            return ""
        raw = resolved.read_text()
    except OSError:
        logger.warning(
            "notes._read_note_snippet: failed to read %s for note_id=%r",
            note.path,
            note.note_id,
        )
        return ""
    try:
        _, body = frontmatter.parse(raw)
    except frontmatter.FrontmatterError:
        # Body still useful even if frontmatter is broken; fall back to raw.
        body = raw
    return body.lstrip()[:_SNIPPET_CHARS]


def _note_to_review_dict(note: Note, vault_root: Path) -> dict:
    """Serialize a Note row to the dict shape returned by review-queue endpoints.

    Skips the full body — only emits a short snippet so the audit/pending
    queues can render without paging large markdown payloads. The Note
    model has no ``tier`` column today, so ``tier`` is omitted entirely;
    if/when one is added, surface it here.
    """
    updated_at = note.updated_at
    return {
        "id": note.note_id,
        "title": note.title,
        "snippet": _read_note_snippet(vault_root, note),
        "visibility": note.visibility,
        "visibility_verified": note.visibility_verified,
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
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


def _write_note_visibility_frontmatter(
    vault_root: Path, note: Note, visibility: str | None
) -> None:
    """Set (or clear) the ``visibility`` key in the note's on-disk frontmatter.

    ``visibility=None`` removes the key entirely so the file shape matches
    a fresh, un-classified note (i.e. ``visibility`` absent rather than
    ``visibility: null``). Re-serializes via :func:`_serialize_frontmatter`
    so the write path matches ``edit_note``'s.

    Raises:
        ValueError: if the vault file is missing or escapes the vault
            root — same identical error contract as :func:`_get_note_or_raise`'s
            "not found" so the router maps it to 404.
    """
    resolved = (vault_root / note.path).resolve()
    if not resolved.is_relative_to(vault_root) or not resolved.is_file():
        raise ValueError(
            f"Note not found on disk: note_id={note.note_id!r} path={note.path!r}"
        )
    existing_raw = resolved.read_text()
    parsed, body = frontmatter.parse(existing_raw)
    parsed.visibility = visibility
    resolved.write_text(_serialize_frontmatter(parsed, body))


def set_note_visibility(
    session: Session,
    note_id: str,
    visibility: str,
    vault_root: Path,
) -> Note:
    """Set ``note.visibility`` (public|private) and mark verified.

    Writes the frontmatter via :func:`_write_note_visibility_frontmatter`
    and flips ``visibility_verified=True`` because the caller (a human)
    explicitly took an action — same convention as ``reject_gap`` /
    ``answer_gap``.

    Raises:
        ValueError: if ``visibility`` is not exactly ``"public"`` or
            ``"private"``, or if ``note_id`` is unknown.
    """
    if visibility not in ("public", "private"):
        raise ValueError(
            f"visibility must be 'public' or 'private', got {visibility!r}"
        )
    note = _get_note_or_raise(session, note_id)
    _write_note_visibility_frontmatter(vault_root, note, visibility)
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
    vault_root: Path,
) -> Note:
    """Clear ``note.visibility`` and ``visibility_verified``.

    Sends the note back to the pending review queue (``visibility IS
    NULL``) by writing the frontmatter without the ``visibility`` key
    and zeroing the DB flags. The audit-mode "no, this decision was
    wrong" path.

    Raises:
        ValueError: if ``note_id`` is unknown.
    """
    note = _get_note_or_raise(session, note_id)
    _write_note_visibility_frontmatter(vault_root, note, None)
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
    vault_root: Path,
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
            .order_by(Note.created_at.asc(), Note.id.asc())
            .limit(limit)
        )
    else:  # "audit" — Literal type guarantees no other value reaches here
        stmt = (
            select(Note)
            .where(Note.visibility.is_not(None))
            .where(Note.visibility_verified.is_(False))
            .order_by(Note.updated_at.desc().nulls_last(), Note.id.desc())
            .limit(limit)
        )
    rows = session.execute(stmt).scalars().all()
    return [_note_to_review_dict(note, vault_root) for note in rows]
