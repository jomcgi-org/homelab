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
import shutil
from datetime import datetime, timezone
from pathlib import Path
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

# Directory under the vault root where soft-deleted notes are parked.
# Both the gardener (knowledge.gardener._EXCLUDED_DIRS) and raw-ingest
# scanner (knowledge.raw_ingest._EXCLUDED_TOP_LEVEL) skip this so the
# moved files don't get re-ingested as fresh raws.
_TRASH_DIR_NAME = "_trash"


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


def _read_note_snippet(vault_root: Path, note: Note) -> str:
    """Return the first ~100 lines of ``note``'s body, capped at 8 KiB.

    Reads the vault file and strips frontmatter. Missing / unreadable
    files return an empty string — review-queue listing is best-effort
    metadata and must never break on a partial vault. The line+byte cap
    is sized to give the audit UI enough context to evaluate a note
    in-place without surfacing the entire body.
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
    head = "\n".join(body.lstrip().splitlines()[:_SNIPPET_MAX_LINES])
    # Byte cap as a backstop: a single very long line shouldn't blow
    # the payload past what the review UI can render comfortably.
    encoded = head.encode("utf-8")
    if len(encoded) > _SNIPPET_MAX_BYTES:
        head = encoded[:_SNIPPET_MAX_BYTES].decode("utf-8", errors="ignore")
    return head


def _note_to_review_dict(note: Note, vault_root: Path) -> dict:
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
        "snippet": _read_note_snippet(vault_root, note),
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
    return [_note_to_review_dict(note, vault_root) for note in rows]


def _trash_filename(slug: str, *, when: datetime, collision_root: Path) -> str:
    """Build a non-colliding ``<ts>-<slug>.md`` filename under ``collision_root``.

    Timestamps are second-resolution Zulu (same format the gap stubs use)
    so a burst of deletes within the same second falls through to the
    ``-1``, ``-2`` counter suffix instead of overwriting prior trash
    entries. ``collision_root`` is the ``_trash`` directory; callers pass
    it so the existence check is correct relative to the actual vault.
    """
    timestamp = when.strftime("%Y%m%dT%H%M%SZ")
    candidate = f"{timestamp}-{slug}.md"
    if not (collision_root / candidate).exists():
        return candidate
    counter = 1
    while True:
        candidate = f"{timestamp}-{slug}-{counter}.md"
        if not (collision_root / candidate).exists():
            return candidate
        counter += 1


def delete_note(session: Session, note_id: str, vault_root: Path) -> Note:
    """Soft-delete a note. Move its file to ``_trash/<ts>-<slug>.md``.

    Stamps ``deleted_at = now()`` and stashes the original vault-relative
    path in ``pre_delete_path`` so :func:`undelete_note` can restore the
    file to exactly where it lived before, no filename-parsing required.
    ``note.path`` is rewritten to point at the trash location so the row
    consistently reflects where the bytes live on disk.

    Missing on-disk file is tolerated: the DB flip still happens (so the
    row drops out of review queries) but no move is attempted. This
    matches :func:`knowledge.gaps._remove_stub_if_present`'s tolerance —
    the disk and DB can drift, and the row is the system of record for
    "the user said delete this".

    Raises:
        ValueError: if ``note_id`` is unknown OR already deleted (caller
            should call ``undelete_note`` instead of double-deleting).
    """
    note = _get_note_or_raise(session, note_id)

    original_relative = note.path
    src = (vault_root / original_relative).resolve()
    moved_src: Path | None = None
    moved_dest: Path | None = None
    if src.is_file() and src.is_relative_to(vault_root):
        trash_dir = vault_root / _TRASH_DIR_NAME
        trash_dir.mkdir(exist_ok=True)
        slug = Path(original_relative).stem
        filename = _trash_filename(
            slug, when=datetime.now(timezone.utc), collision_root=trash_dir
        )
        dest = trash_dir / filename
        shutil.move(str(src), str(dest))
        moved_src, moved_dest = src, dest
        note.path = str(dest.relative_to(vault_root))
    else:
        # File already gone (raced delete, partial vault, etc.). Keep
        # the existing ``note.path`` so undelete still has a meaningful
        # pre_delete_path to restore to. Don't fabricate a trash entry
        # -- there's nothing to move.
        logger.info(
            "notes.delete_note: file %s missing on disk for note_id=%r; "
            "DB-only soft-delete",
            original_relative,
            note_id,
        )

    note.pre_delete_path = original_relative
    note.deleted_at = datetime.now(timezone.utc)
    session.add(note)
    try:
        session.commit()
    except Exception:
        # DB commit failed AFTER we already moved the file. Roll the
        # filesystem back so the on-disk state still matches the
        # persisted DB row (which now reverts to pointing at the
        # original path). Without this we'd leave the file orphaned in
        # _trash/ with the live row claiming it's at the original path.
        if moved_src is not None and moved_dest is not None and moved_dest.exists():
            shutil.move(str(moved_dest), str(moved_src))
        raise
    session.refresh(note)
    logger.info(
        "notes.delete_note: soft-deleted note_id=%r original_path=%s trash_path=%s",
        note_id,
        original_relative,
        note.path,
    )
    return note


def undelete_note(session: Session, note_id: str, vault_root: Path) -> Note:
    """Undo a soft-delete: move the file back, clear the bookkeeping.

    Reads ``pre_delete_path`` (set by :func:`delete_note`) to know where
    to restore the file. If the original location is already occupied
    (rare — usually because a brand-new note got written there in the
    interim), falls back to a counter suffix so the undelete never
    silently overwrites unrelated data.

    Raises:
        ValueError: if ``note_id`` is unknown, or if the row is not in
            soft-deleted state. Both messages start with ``"Note not
            found"`` so :func:`router._map_note_error` maps to 404.
    """
    # Bypass _get_note_or_raise's deleted-as-404 so we can find the row.
    note = session.exec(  # nosemgrep: sqlmodel-select-missing-deleted-at-filter (restore_note must find soft-deleted rows — that is its purpose)
        select(Note).where(Note.note_id == note_id)
    ).one_or_none()
    if note is None:
        raise ValueError(f"Note not found: note_id={note_id!r}")
    if note.deleted_at is None:
        raise ValueError(f"Note not found: note_id={note_id!r} is not deleted")

    # pre_delete_path SHOULD always be set by delete_note, but tolerate
    # the legacy edge case where a row was somehow marked deleted without
    # one (manual DB poke). Fall back to whatever path is currently set.
    original_relative = note.pre_delete_path or note.path
    trash_src = (vault_root / note.path).resolve()
    dest = (vault_root / original_relative).resolve()
    if trash_src.is_file() and trash_src.is_relative_to(vault_root):
        # Resolve collision at the destination: a brand-new note may have
        # claimed the slug while this one was in trash. Use a counter
        # suffix on the FILENAME stem (matching create_note's strategy).
        if dest.exists():
            stem = dest.stem
            parent = dest.parent
            counter = 1
            while True:
                candidate = parent / f"{stem}-{counter}.md"
                if not candidate.exists():
                    dest = candidate
                    break
                counter += 1
            logger.warning(
                "notes.undelete_note: original path %s occupied; "
                "restoring note_id=%r to %s instead",
                original_relative,
                note_id,
                dest.relative_to(vault_root),
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(trash_src), str(dest))
        note.path = str(dest.relative_to(vault_root))
    else:
        # Nothing to move on disk; just clear the DB flags so the row
        # reappears in queries.
        logger.info(
            "notes.undelete_note: trash file %s missing for note_id=%r; "
            "DB-only undelete",
            note.path,
            note_id,
        )
        note.path = original_relative

    note.deleted_at = None
    note.pre_delete_path = None
    session.add(note)
    session.commit()
    session.refresh(note)
    logger.info(
        "notes.undelete_note: restored note_id=%r path=%s",
        note_id,
        note.path,
    )
    return note
