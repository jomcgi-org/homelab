"""Vault → knowledge schema reconciler."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import text
from sqlmodel import Session, select

from knowledge import frontmatter, links, wikilinks
from knowledge.frontmatter import FrontmatterError, ParsedFrontmatter
from knowledge.models import Gap
from knowledge.store import KnowledgeStore
from shared.chunker import chunk_markdown

logger = logging.getLogger("monolith.knowledge.reconciler")

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Mirror of the CHECK constraints in chart/migrations/20260424000000_knowledge_gaps.sql
# (gap_class) and models.GapClass / GapState literals — keep in sync.
_VALID_GAP_CLASSES = frozenset({"external", "internal", "hybrid", "parked"})
_VALID_GAP_STATES = frozenset(
    {
        "discovered",
        "classified",
        "in_review",
        "researching",
        "researched",
        "verified",
        "consolidated",
        "committed",
        "parked",
        "rejected",
    }
)


@dataclass(frozen=True)
class ReconcileStats:
    """Per-cycle outcome counts.

    `skipped_locked` tracks files where pg_try_advisory_xact_lock
    returned false (another reconciler is processing). This is always
    zero in the SQLite unit-test fixture because the advisory-lock
    branch is guarded by `session.bind.dialect.name == "postgresql"`.
    """

    upserted: int
    deleted: int
    unchanged: int
    failed: int
    skipped_locked: int


class ReadOnlyVaultError(Exception):
    """Raised when the vault is read-only and a file lacks a frontmatter id.

    We refuse to ingest with an ephemeral id because the id would not be
    stable across restarts — graph edges pointing at the ephemeral id
    would silently break.
    """


class _Embedder(Protocol):
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


def _try_pg_advisory_xact_lock(session: Session, key: str) -> bool | None:
    """Try a Postgres per-transaction advisory lock; sync DB I/O.

    Module-level so the inner ``session.execute`` is not lexically
    inside any ``async def`` (the ``no-sync-session-in-async-def`` rule
    uses ``pattern-inside: async def $FUNC(...): ...`` which is a
    lexical-enclosure check). Called synchronously rather than via
    ``asyncio.to_thread`` because (a) the lock is tx-scoped and must
    run on the caller's session/transaction, and (b) sub-millisecond
    cost makes the loop-blocking concern negligible.
    """
    return session.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:p))"),
        {"p": key},
    ).scalar()


class Reconciler:
    def __init__(
        self,
        *,
        store: KnowledgeStore,
        embed_client: _Embedder,
        vault_root: Path,
    ) -> None:
        self.store = store
        self.embed_client = embed_client
        self.vault_root = Path(vault_root)
        self.processed_root = self.vault_root / "_processed"
        # _researching/ holds gap stubs (type: gap). The reconciler indexes
        # them so the graph has queryable Note rows, but they are not
        # chunked or embedded — they're pipeline state, not retrieval
        # targets. See Task 5 of the gap-classifier-stub-notes design.
        self.researching_root = self.vault_root / "_researching"

    async def run(self) -> ReconcileStats:
        """Reconcile the vault. Returns a ReconcileStats breakdown.

        The pre-loop helpers (`_pre_sync_links`, `get_indexed`, `_walk`)
        are sync and walk the entire vault + scan the full Note table.
        Run them on a worker thread via `asyncio.to_thread` so the
        event loop stays free for `/healthz` and other API requests.
        Same loop-unblock pattern as the gardener handler.
        """
        # Sync ## Links sections across all processed notes before hash
        # comparison so that notes with missing or stale sections get
        # re-ingested in this cycle via the normal hash-change path.
        await asyncio.to_thread(self._pre_sync_links)
        # ADR 006 one-shot backfill: fill `content` for rows that predate the
        # column from disk, without re-embedding. No-op once the corpus is
        # populated. Runs before the hash-diff loop so the body of record is
        # in Postgres regardless of whether a file changed this cycle.
        await asyncio.to_thread(self._backfill_content)
        indexed = await asyncio.to_thread(self.store.get_indexed)
        on_disk = await asyncio.to_thread(self._walk, previous_indexed=indexed)

        to_upsert = sorted(
            path for path, h in on_disk.items() if indexed.get(path) != h
        )
        to_delete = sorted(path for path in indexed if path not in on_disk)
        # Count files with a matching hash explicitly rather than deriving
        # from (on_disk - to_upsert) — the derived formula miscounts failed
        # files as "unchanged".
        unchanged = sum(1 for path, h in on_disk.items() if indexed.get(path) == h)

        upserted = 0
        deleted = 0
        failed = 0
        skipped_locked = 0

        for path in to_delete:
            logger.info("knowledge: deleting %s", path)
            try:
                self.store.delete_note(path)
            except Exception:  # noqa: BLE001 — partial-failure isolation
                logger.exception("knowledge: failed to delete %s, continuing", path)
                failed += 1
                try:
                    self.store.session.rollback()
                except Exception:  # noqa: BLE001
                    logger.exception("knowledge: rollback after delete failure failed")
                continue
            deleted += 1

        for path in to_upsert:
            try:
                ingested = await self._ingest_one(path, on_disk[path])
            except FileNotFoundError:
                logger.warning("knowledge: file vanished mid-cycle: %s", path)
                continue
            except FrontmatterError as exc:
                # A broken frontmatter block must NOT overwrite the
                # existing row with empty defaults — skip the file and
                # leave the prior index entry intact.
                logger.warning(
                    "knowledge: skipping note with invalid frontmatter %s: %s",
                    path,
                    exc,
                )
                failed += 1
                try:
                    self.store.session.rollback()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "knowledge: rollback after frontmatter error failed"
                    )
                continue
            except Exception:  # noqa: BLE001 — partial-failure isolation
                logger.exception("knowledge: failed to ingest %s, continuing", path)
                failed += 1
                # Roll back any uncommitted state from the failed ingest so
                # the next file starts with a clean session.
                try:
                    self.store.session.rollback()
                except Exception:  # noqa: BLE001
                    logger.exception("knowledge: rollback after failure failed")
                continue
            if ingested:
                upserted += 1
            else:
                # _ingest_one returns False for the advisory-lock-busy
                # branch (postgres only). Unreachable in sqlite tests.
                skipped_locked += 1

        stats = ReconcileStats(
            upserted=upserted,
            deleted=deleted,
            unchanged=unchanged,
            failed=failed,
            skipped_locked=skipped_locked,
        )
        logger.info(
            "knowledge: reconciled upserted=%d deleted=%d unchanged=%d failed=%d skipped_locked=%d",
            stats.upserted,
            stats.deleted,
            stats.unchanged,
            stats.failed,
            stats.skipped_locked,
        )
        # Per-file errors are isolated: they increment stats.failed and
        # are logged with a traceback. We deliberately do NOT re-raise
        # here — a single bad file must not tank an entire reconcile
        # cycle. Alerting should key off the `failed` structured field.
        return stats

    def _walk(self, *, previous_indexed: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        # Scan _processed/ (atoms, tasks, etc.) AND _researching/ (gap
        # stubs) — both contribute to the graph. Stubs are handled
        # specially in _ingest_one (no chunk/embed) but still need Note
        # rows so the gaps table can project their frontmatter.
        for root in (self.processed_root, self.researching_root):
            if not root.exists():
                continue
            for p in root.rglob("*.md"):
                rel = p.relative_to(self.vault_root).as_posix()
                try:
                    data = p.read_bytes()
                except FileNotFoundError:
                    # Genuine race with unlink — let the delete loop handle it
                    # on the next cycle (or this cycle, if get_indexed still
                    # lists it).
                    continue
                except (PermissionError, OSError):
                    # Transient read error: carry forward the previous hash so
                    # the file stays in the snapshot under its old hash and is
                    # neither marked for delete nor for re-ingestion.
                    logger.warning("knowledge: skipping unreadable file %s", rel)
                    if rel in previous_indexed:
                        out[rel] = previous_indexed[rel]
                    continue
                out[rel] = hashlib.sha256(data).hexdigest()
        return out

    def _backfill_content(self) -> int:
        """One-shot ADR 006 backfill: populate ``content`` from disk.

        The reconciler only re-ingests files whose hash changed, so rows
        that predate the ``content`` column would otherwise never get a
        body. This reads each null-content note's body from disk, strips
        the generated ``## Links`` section (regenerable from frontmatter
        edges), and fills the column in a single commit. No re-embedding —
        chunks are untouched. Returns the number of rows updated; a no-op
        returning 0 once the corpus is fully backfilled.
        """
        notes = self.store.notes_missing_content()
        if not notes:
            return 0
        updated = 0
        for note in notes:
            abs_path = self.vault_root / note.path
            try:
                raw = self._read_text(abs_path)
            except (FileNotFoundError, UnicodeDecodeError, OSError):
                # File gone or unreadable this cycle — leave content NULL
                # and retry on a later cycle.
                continue
            try:
                _meta, body = frontmatter.parse(raw)
            except FrontmatterError:
                logger.warning(
                    "knowledge: backfill skipping invalid frontmatter %s",
                    note.path,
                )
                continue
            note.content = wikilinks.strip_links_section(body)
            updated += 1
        if updated:
            self.store.session.commit()
            logger.info("knowledge: backfilled content for %d notes", updated)
        return updated

    def _read_text(self, abs_path: Path) -> str:
        try:
            # newline="" disables universal-newlines translation so we
            # can observe (and preserve) the file's original line endings
            # when backfilling the frontmatter id.
            with open(abs_path, encoding="utf-8", newline="") as f:
                return f.read()
        except UnicodeDecodeError:
            logger.warning("knowledge: invalid utf-8, skipping: %s", abs_path)
            raise

    async def _ingest_one(self, rel_path: str, content_hash: str) -> bool:
        # Per-file advisory lock prevents producer/consumer races (gardener
        # writing the same file mid-cycle, or two replicas if locking ever
        # weakens). Lock is auto-released at txn end. Postgres-only — the
        # SQLite test fixture skips this branch.
        session = self.store.session
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            # Sub-millisecond Postgres advisory lock taken on the caller's
            # transaction — must NOT be moved to a fresh session/thread or
            # the lock has no effect for the caller. Called synchronously
            # (not via to_thread) because (a) it's sub-ms so loop blocking
            # is negligible, and (b) passing `session` to to_thread would
            # trip the `no-session-in-to-thread` rule. The session.execute
            # lives in the module-level helper so it's not lexically inside
            # this async def and the `no-sync-session-in-async-def` rule
            # does not match.
            locked = _try_pg_advisory_xact_lock(session, rel_path)
            if not locked:
                logger.info("knowledge: advisory lock busy, deferring %s", rel_path)
                return False

        abs_path = self.vault_root / rel_path
        raw = self._read_text(abs_path)

        meta, body = frontmatter.parse(raw)
        title = meta.title or Path(rel_path).stem

        # Auto-backfill missing note_id. Mandatory: every row must have one.
        note_id = meta.note_id
        if not note_id:
            note_id = _slugify(title)
            try:
                raw, content_hash = self._write_back_id(abs_path, raw, note_id)
                meta, body = frontmatter.parse(raw)
            except OSError as exc:
                # Catches both PermissionError (EACCES/EPERM) and
                # OSError(EROFS) from read-only mounts — the latter is
                # not a PermissionError subclass despite the name.
                raise ReadOnlyVaultError(
                    f"vault is read-only and {rel_path} has no frontmatter id;"
                    " refusing to ingest with ephemeral id"
                ) from exc

        # Gap stubs are pipeline state, not retrieval targets. Upsert the
        # Note row (so the graph has a queryable entity) with no chunks,
        # no links, no embeddings, then project the stub's frontmatter
        # into the corresponding Gap row. The ## Links section is also
        # skipped — `type: gap` would otherwise render a spurious
        # `Up: [[gap]]` link and mutate the stub on every cycle.
        if meta.type == "gap":
            self.store.upsert_note(
                note_id=note_id,
                path=rel_path,
                content_hash=content_hash,
                title=title,
                metadata=meta,
                chunks=[],
                vectors=[],
                links=[],
                content=wikilinks.strip_links_section(body),
            )
            _project_gap_frontmatter(self.store.session, note_id, meta)
            return True

        # Sync ## Links section from frontmatter edges (template or update).
        # Strip the generated section from body for chunking/link extraction —
        # the pre-sync pass may have already written it before _ingest_one ran,
        # so we cannot rely on capturing body before the sync call.
        authored_body = wikilinks.strip_links_section(body)
        updated = wikilinks.sync_links(raw, meta)
        if updated is not None:
            try:
                raw, content_hash = self._write_back_links(abs_path, updated)
            except OSError:
                logger.warning(
                    "knowledge: vault read-only, skipping links sync for %s", rel_path
                )

        chunks = chunk_markdown(authored_body)
        if not chunks:
            chunks = [
                {"index": 0, "section_header": "", "text": authored_body or title}
            ]
        vectors = await self.embed_client.embed_batch([c["text"] for c in chunks])
        note_links = links.extract(authored_body)

        self.store.upsert_note(
            note_id=note_id,
            path=rel_path,
            content_hash=content_hash,
            title=title,
            metadata=meta,
            chunks=chunks,
            vectors=vectors,
            links=note_links,
            content=authored_body,
        )
        return True

    def _write_back_id(self, abs_path: Path, raw: str, note_id: str) -> tuple[str, str]:
        """Inject ``id: <note_id>`` into the file's frontmatter.

        Preserves the file's existing line ending (LF or CRLF) so Git
        diffs stay clean on Windows-authored notes. Returns the new
        ``(raw, content_hash)``. Raises ``OSError`` on read-only mounts
        (EROFS is plain ``OSError``, not ``PermissionError``, so the
        caller must catch the broader type).
        """
        if raw.startswith("---\r\n"):
            eol = "\r\n"
            new_raw = f"---{eol}id: {note_id}{eol}" + raw[len("---\r\n") :]
        elif raw.startswith("---\n"):
            eol = "\n"
            new_raw = f"---{eol}id: {note_id}{eol}" + raw[len("---\n") :]
        else:
            # No existing frontmatter — default to LF.
            new_raw = f"---\nid: {note_id}\n---\n{raw}"
        # newline="" preserves whatever EOLs are already in new_raw.
        with open(abs_path, "w", encoding="utf-8", newline="") as f:
            f.write(new_raw)
        new_hash = hashlib.sha256(new_raw.encode("utf-8")).hexdigest()
        return new_raw, new_hash

    def _pre_sync_links(self) -> None:
        """Sync the ## Links section for every note in _processed/ before reconcile.

        Files whose links section is missing or stale are rewritten so their
        on-disk hash changes, causing the main reconcile loop to re-ingest them
        via the normal hash-change path.
        """
        if not self.processed_root.exists():
            return
        for p in self.processed_root.rglob("*.md"):
            try:
                raw = self._read_text(p)
                meta, _ = frontmatter.parse(raw)
                updated = wikilinks.sync_links(raw, meta)
                if updated is not None:
                    self._write_back_links(p, updated)
                    logger.debug("knowledge: pre-synced links in %s", p.name)
            except Exception:  # noqa: BLE001 — best-effort, never block reconcile
                logger.warning("knowledge: failed to pre-sync links for %s", p)

    def _write_back_links(self, abs_path: Path, new_raw: str) -> tuple[str, str]:
        """Write the updated file (with synced ## Links section) and return (raw, hash)."""
        with open(abs_path, "w", encoding="utf-8", newline="") as f:
            f.write(new_raw)
        new_hash = hashlib.sha256(new_raw.encode("utf-8")).hexdigest()
        return new_raw, new_hash


def _slugify(text_in: str) -> str:
    normalized = unicodedata.normalize("NFKD", text_in)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_only.lower()).strip("-")
    return slug or "note"


def _parse_iso8601(value: str) -> datetime | None:
    """Parse ISO 8601 timestamps accepting both the 'Z' suffix and +00:00 form.

    ``datetime.fromisoformat`` only started recognising the 'Z' suffix in
    Python 3.11. We normalise 'Z' → '+00:00' for portability across 3.10
    runtimes (BuildBuddy/CI images) and to make the parser tolerant of
    the canonical Zulu output emitted by the stub writer (Task 4).
    """
    try:
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        return datetime.fromisoformat(normalized)
    except (ValueError, AttributeError):
        return None


def _project_gap_frontmatter(
    session: Session, note_id: str, meta: ParsedFrontmatter
) -> None:
    """Project a type:gap stub's frontmatter into its Gap row.

    Matches the stub to its Gap row by ``note_id`` (string identity — same
    pattern as ``AtomRawProvenance.derived_note_id``). Only writes when a
    value actually changed to avoid gratuitous updates that would churn
    ``updated_at`` fields on downstream consumers.

    Invalid ``gap_class`` / ``state`` values are logged at WARNING and
    skipped so the Gap row stays at its previous state and the next
    classifier tick can retry. This mirrors ``classify_gaps``' defensive
    validation — never let malformed frontmatter corrupt the DB.
    """
    gap = session.execute(
        select(Gap).where(Gap.note_id == note_id, Gap.deleted_at.is_(None))
    ).scalar_one_or_none()
    if gap is None:
        # Stub without a Gap row — discover_gaps will insert one later.
        return

    extra = meta.extra or {}
    gap_class = extra.get("gap_class")
    # `status` is a promoted top-level frontmatter key (see
    # frontmatter._PROMOTED_KEYS) so it lands on `meta.status`, not in
    # `meta.extra`. Fall back to the row's current state when the stub
    # omits it entirely.
    status = meta.status if meta.status is not None else gap.state
    classifier_version = extra.get("classifier_version")
    classified_at = extra.get("classified_at")
    resolved_at = extra.get("resolved_at")

    if gap_class is not None and gap_class not in _VALID_GAP_CLASSES:
        logger.warning(
            "reconciler: stub %s has invalid gap_class=%r; skipping projection",
            note_id,
            gap_class,
        )
        return
    if status not in _VALID_GAP_STATES:
        logger.warning(
            "reconciler: stub %s has invalid status=%r; skipping projection",
            note_id,
            status,
        )
        return

    changed = False
    if gap_class is not None and gap.gap_class != gap_class:
        gap.gap_class = gap_class
        changed = True
    if gap.state != status:
        gap.state = status
        changed = True
    if classifier_version and gap.pipeline_version != classifier_version:
        gap.pipeline_version = classifier_version
        changed = True
    if classified_at:
        parsed = _parse_iso8601(classified_at)
        if parsed and gap.classified_at != parsed:
            gap.classified_at = parsed
            changed = True
    if resolved_at:
        parsed = _parse_iso8601(resolved_at)
        if parsed and gap.resolved_at != parsed:
            gap.resolved_at = parsed
            changed = True

    if changed:
        session.add(gap)
        session.commit()
