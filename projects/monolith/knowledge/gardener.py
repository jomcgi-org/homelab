"""Knowledge gardener — decomposes raw vault notes into typed knowledge artifacts."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml
from sqlalchemy import and_, not_, or_
from sqlmodel import Session, select

from knowledge import frontmatter
from knowledge.models import AtomRawProvenance, Note, RawInput
from knowledge.visibility import VISIBILITY_CRITERIA

logger = logging.getLogger("monolith.knowledge.gardener")

_EXCLUDED_DIRS = {
    "_processed",
    "_raw",
    "_researching",
    # Soft-deleted notes are moved here by knowledge.notes.delete_note.
    # Skipping the directory prevents the gardener from re-ingesting
    # trashed notes as fresh raws on the next discover tick.
    "_trash",
    ".obsidian",
    ".trash",
}

# Version stamp recorded on every provenance row the gardener produces.
# Bump this when the prompt or model changes to trigger a manual reprocess.
GARDENER_VERSION = "claude-sonnet-4-6@v1"

_SLUG_RE = re.compile(r"[^a-z0-9]+")

_CLAUDE_PROMPT_HEADER = """\
You are a knowledge gardener. Decompose a raw note into atomic knowledge artifacts.

Source raw_id: {raw_id}
Include `derived_from_raw: {raw_id}` as a frontmatter field in every note you create.

Steps:
1. Read the raw note from {raw_file_path} using the Read tool.
2. Run `knowledge-search "<topic>"` (Bash) to find related existing notes.
3. Read related notes from {processed_root}/ using the Read tool.
4. Create each atomic note as a new file in {processed_root}/ using the Write tool.
   Allowed types: atom (concept/principle), fact (verifiable claim), active (journal/TODO).

   For `active` (task) notes, include these additional frontmatter fields:
   - status: active | someday | blocked   (required for tasks)
   - size: small | medium | large | unknown   (estimate complexity; use unknown if ambiguous)
   - due: <ISO date or omit>   (only if a deadline is mentioned or implied)
   - blocked-by: [<note-ids>]   (only if the task depends on another specific piece of work)

   Size estimation guide:
   - small: single-step, config change, no dependencies
   - medium: multi-step but well-understood, few edges
   - large: cross-cutting, multiple dependencies, significant scope
   - unknown: ambiguous — flag for manual review

   Recognise task-shaped content: phrases like "should deploy", "need to", "TODO",
   "blocked on", "once X lands" indicate actionable work that should become an active note.

5. Each file must start with YAML frontmatter:
---
id: <slug-of-title>
title: "<concise title — MUST be quoted if it contains a colon>"
aliases: [<optional list of alternative wikilink forms — see Aliases section below>]
type: atom|fact|active
derived_from_raw: {raw_id}
visibility: public|private   # REQUIRED — see criteria below
tags: [optional]
edges:
  derives_from: [source-slug]   # allowed edge types: derives_from | refines | generalizes | related | contradicts | supersedes
---
<markdown body>
   IMPORTANT: Always wrap the title value in double quotes to avoid YAML parse errors
   (e.g. `title: "Atomic Note: One Concept"`, NOT `title: Atomic Note: One Concept`).
   IMPORTANT: Do NOT prefix titles with category labels like "(Book)", "(Concept)", etc.
   The `type` field already captures the category. The title should be the concept itself.
   IMPORTANT: The filename MUST be `<id>.md` — i.e. the slugified title. If the id is
   `staff-engineers-path`, the file must be `{processed_root}/staff-engineers-path.md`.
6. Patch edges on related existing notes using the Edit tool.
7. Each note covers exactly one concept. Prefer many small notes over one large note.

## Aliases (frontmatter field)

The `aliases:` field lists alternative human-readable forms of the title that
should resolve to this same atom in Obsidian. It also feeds the gap-classifier:
when a referencing note contains `[[Some Title]]`, the classifier checks both
`_processed/<slug>.md` filenames AND existing atoms' `aliases:` lists before
queueing a gap. Every variant of the title that shows up as a wikilink in
referencing notes should appear here.

When CREATING a new atom, populate `aliases:` with any of:
- The title-cased form if it differs from the slugified id
- Possessive variants (`Bayes's Theorem`, `Bayes' Theorem`)
- Plural/singular alternates (`Blameless Postmortem`, `Blameless Postmortems`)
- Article variations (`The Software Engineer's Guidebook`)
- Common abbreviations or expansions (`DORA Metrics` ↔ `Four Key DORA Metrics`)

Omit the field (or use `[]`) if the slug already matches the only wikilink form.

When UPDATING an existing atom (i.e. when this raw mentions a concept that
already exists at `{processed_root}/<slug>.md`), use the **Edit** tool — never
Write. Edit lets you modify specific sections (body, individual frontmatter
fields) without touching `aliases:` or any other user-curated frontmatter.
Write would overwrite the whole file and silently strip fields that aren't
in this prompt's schema, which is a real bug we are explicitly guarding
against. If you genuinely need to rewrite an atom from scratch, first Read
its current frontmatter, copy the `aliases:` array verbatim into your new
content, then Write — never lose alias entries.

## Linking to neighboring concepts (forward links)

When the body of an atom names another distinct concept — a named tool,
heuristic, person, framework, book, method, or term that could stand on
its own as an atom — wrap it in `[[Concept Name]]`. Examples:

- "Inspired by [[Conway's Law]], we split the team along the seam..."
- "We measured this with [[DORA Metrics]] over six weeks."
- "Borrowed the framing from [[The Staff Engineer's Path]]."

Body wikilinks are the gardener's primary mechanism for organic graph
growth. Unresolved links queue as gaps and feed the external-research
pipeline — so forward-linking is how a concept earns the right to exist
on its own atom. This complements `aliases:` (which handles inbound
resolution from other notes) and typed `edges:` (which captures
structured semantic relations).

Guardrails — DO wikilink:
- Named concepts that could each be their own atom (proper nouns,
  named heuristics, named tools, named frameworks, book titles)
- The load-bearing 3–5 references per atom — pick the ones a future
  reader would want to navigate to

Guardrails — DON'T wikilink:
- Generic words ("the team", "the meeting", "yesterday", "production")
- Multi-clause phrases — wikilinks must name a single concept
- Restatements of the atom's own title
- Every domain noun that happens to appear — quality over quantity

If a wikilink target already exists at `{processed_root}/<slug>.md`,
prefer that exact slug as the link target. If it doesn't exist, write
the natural title-cased form — the gap-classifier and aliases system
handle resolution.

{VISIBILITY_CRITERIA}

Title: {title}

""".replace("{VISIBILITY_CRITERIA}", VISIBILITY_CRITERIA)

_DISTILL_PROMPT = """\
You are a knowledge gardener. A task has been completed. Extract any reusable
learnings, patterns, or facts from the task note into new atomic knowledge artifacts.

Completed task: {note_id}

Steps:
1. Read the completed task note from {note_path} using the Read tool.
2. Identify any reusable learnings, patterns, gotchas, or facts worth preserving.
3. If there are learnings worth preserving, create atomic notes in {processed_root}/.
4. If the task was routine with no notable learnings, create no new notes.
5. Each new note must have YAML frontmatter with:
   - id, title, type (atom or fact), tags
   - visibility: public|private   # REQUIRED — see criteria below
   - edges: derives_from: [{note_id}]
6. Do NOT create a new active/task note. Only create atom or fact notes.

## Linking to neighboring concepts (forward links)

When the body of a distilled atom names another distinct concept — a
named tool, heuristic, framework, book, or term that could stand on its
own — wrap it in `[[Concept Name]]`. Body wikilinks queue as gaps and
feed the external-research pipeline; forward-linking is how the graph
grows organically from each new atom.

Wikilink load-bearing concepts only (3–5 max per atom). Skip generic
words ("the team", "production"), multi-clause phrases, and the atom's
own title. If a target already exists at `{processed_root}/<slug>.md`,
prefer that slug; otherwise write the natural title-cased form.

{VISIBILITY_CRITERIA}

Title: {title}

""".replace("{VISIBILITY_CRITERIA}", VISIBILITY_CRITERIA)

_CLAUDE_TIMEOUT_SECS = 900


def _slugify(text_in: str) -> str:
    normalized = unicodedata.normalize("NFKD", text_in)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_only.lower()).strip("-")
    return slug or "note"


@dataclass(frozen=True)
class GardenStats:
    ingested: int
    failed: int
    moved: int = 0
    deduped: int = 0
    reconciled: int = 0
    resolved: int = 0
    distilled: int = 0
    consolidated: int = 0
    gaps_discovered: int = 0


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    """Return (meta_dict, body) split from a markdown file's frontmatter.

    Returns ({}, raw) if no frontmatter block is present.
    """
    if not raw.startswith("---"):
        return {}, raw
    # Find the closing --- on its own line
    lines = raw.splitlines(keepends=True)
    if not lines or not lines[0].rstrip("\r\n") == "---":
        return {}, raw
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, raw
    block = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1 :])
    try:
        meta = yaml.safe_load(frontmatter._sanitize_yaml_block(block)) or {}
    except yaml.YAMLError:
        return {}, raw
    if not isinstance(meta, dict):
        return {}, raw
    return meta, body


_DEFAULT_MAX_FILES_PER_RUN = 10


class Gardener:
    _MAX_RETRIES = 3

    def __init__(
        self,
        *,
        vault_root: Path,
        max_files_per_run: int = _DEFAULT_MAX_FILES_PER_RUN,
        claude_bin: str = "claude",
        session: Session | None = None,
    ) -> None:
        self.vault_root = Path(vault_root)
        # Cap the number of raw files processed per cycle. The claude subprocess
        # can take up to _CLAUDE_TIMEOUT_SECS per file, so an uncapped cycle
        # over a large vault could run for hours. A value <= 0 disables the cap.
        self.max_files_per_run = max_files_per_run
        self.claude_bin = claude_bin
        self.session = session
        self._last_stdout: bytes = b""
        self.processed_root = self.vault_root / "_processed"

    def _resolve_pending_provenance(self) -> int:
        """Upgrade pending provenance rows by resolving derived_note_id to atom_fk."""
        if self.session is None:
            return 0
        pending = self.session.exec(
            select(AtomRawProvenance).where(
                and_(
                    AtomRawProvenance.atom_fk.is_(None),
                    AtomRawProvenance.derived_note_id.is_not(None),
                )
            )
        ).all()
        resolved = 0
        for row in pending:
            note = self.session.exec(
                select(Note).where(Note.note_id == row.derived_note_id)
            ).first()
            if note is None:
                continue
            row.atom_fk = note.id
            row.derived_note_id = None
            self.session.add(row)
            resolved += 1
        return resolved

    def _raws_needing_decomposition(self) -> list[RawInput]:
        """Return fresh raws followed by retriable failed raws.

        Tier 1 — fresh: no current-version or pre-migration provenance at all.
        Tier 2 — retriable: have a ``derived_note_id='failed'`` provenance row
        with ``retry_count < _MAX_RETRIES``.

        Returns ``fresh + retriable``.
        """
        if self.session is None:
            return []

        # Subquery: raw_fk values that have current-version or pre-migration
        # provenance (i.e. successfully handled or grandfathered).
        handled_subq = (
            select(AtomRawProvenance.raw_fk)
            .where(AtomRawProvenance.raw_fk.is_not(None))
            .where(
                or_(
                    AtomRawProvenance.gardener_version == GARDENER_VERSION,
                    AtomRawProvenance.gardener_version == "pre-migration",
                )
            )
            .where(
                or_(
                    AtomRawProvenance.derived_note_id.is_(None),
                    AtomRawProvenance.derived_note_id != "failed",
                )
            )
            .subquery()
        )

        # Subquery: raw_fk values that have a "failed" provenance row
        # (regardless of retry_count — we filter retry_count in tier 2).
        failed_subq = (
            select(AtomRawProvenance.raw_fk)
            .where(AtomRawProvenance.raw_fk.is_not(None))
            .where(AtomRawProvenance.derived_note_id == "failed")
            .subquery()
        )

        # Tier 1: fresh raws — not handled AND not failed.
        fresh_stmt = (
            select(RawInput)
            .where(not_(RawInput.id.in_(select(handled_subq.c.raw_fk))))
            .where(not_(RawInput.id.in_(select(failed_subq.c.raw_fk))))
            .order_by(RawInput.created_at.asc().nullslast(), RawInput.id.asc())
        )
        fresh = list(self.session.exec(fresh_stmt).all())

        # Tier 2: retriable failed raws — have a "failed" row with
        # retry_count < _MAX_RETRIES and no successful current-version prov.
        retriable_subq = (
            select(AtomRawProvenance.raw_fk)
            .where(AtomRawProvenance.raw_fk.is_not(None))
            .where(AtomRawProvenance.derived_note_id == "failed")
            .where(AtomRawProvenance.retry_count < self._MAX_RETRIES)
            .subquery()
        )
        retriable_stmt = (
            select(RawInput)
            .where(RawInput.id.in_(select(retriable_subq.c.raw_fk)))
            .where(not_(RawInput.id.in_(select(handled_subq.c.raw_fk))))
            .order_by(RawInput.created_at.asc().nullslast(), RawInput.id.asc())
        )
        retriable = list(self.session.exec(retriable_stmt).all())

        return fresh + retriable

    async def run(self) -> GardenStats:
        """Run one gardening cycle: resolve pending → move → reconcile → decompose.

        All sync helpers (DB scans, filesystem walks, slug-folding loops)
        run on a worker thread via ``asyncio.to_thread`` so the event loop
        stays free for the FastAPI ``/healthz`` probe and other API
        requests. Without this, a single tick can hold the loop for
        seconds-to-minutes; the K8s liveness probe (default
        ``timeoutSeconds: 1``) would then fire and the pod would restart
        mid-cycle. The Claude subprocess calls (``_ingest_one`` and
        ``_distill_completed_tasks``) are already async via
        ``asyncio.create_subprocess_exec`` and yield naturally.
        """
        from knowledge.raw_ingest import move_phase, reconcile_raw_phase

        # Run every cycle: the Gardener instance is reconstructed per scheduled
        # tick (see service.py garden_handler), so an instance flag would have
        # no effect. Bounded cost on healthy vaults — the helper's
        # byte-comparison idempotency short-circuits when nothing changed, so
        # clean stubs aren't rewritten and mtimes are preserved. Wide
        # try/except so this backfill cannot break a gardening cycle.
        try:
            from knowledge.gap_stubs import dedupe_stub_frontmatter

            cleaned = await asyncio.to_thread(dedupe_stub_frontmatter, self.vault_root)
            if cleaned:
                logger.info(
                    "knowledge.garden: deduped %d stub frontmatters",
                    cleaned,
                )
        except Exception:
            logger.exception(
                "knowledge.garden: stub frontmatter dedup failed (non-fatal)"
            )

        resolved_count = await asyncio.to_thread(self._resolve_pending_provenance)
        if resolved_count and self.session is not None:
            self.session.commit()

        now = datetime.now(timezone.utc)
        move_stats = await asyncio.to_thread(
            move_phase, vault_root=self.vault_root, now=now
        )

        reconcile_stats = None
        if self.session is not None:
            reconcile_stats = await asyncio.to_thread(
                reconcile_raw_phase,
                vault_root=self.vault_root,
                session=self.session,
            )
            self.session.commit()

        raws = await asyncio.to_thread(self._raws_needing_decomposition)
        if self.max_files_per_run > 0 and len(raws) > self.max_files_per_run:
            logger.info(
                "gardener: %d raws need decomposition, capping to %d",
                len(raws),
                self.max_files_per_run,
            )
            raws = raws[: self.max_files_per_run]

        ingested = 0
        failed = 0
        for raw in raws:
            try:
                await self._ingest_one(self.vault_root / raw.path)
                ingested += 1
            except Exception:
                logger.exception("gardener: failed to ingest %s", raw.path)
                failed += 1

        distilled, distill_failed = await self._distill_completed_tasks()
        failed += distill_failed

        consolidated = await asyncio.to_thread(self._consolidate_task_views)

        gaps_discovered = await asyncio.to_thread(self._discover_gaps)

        stats = GardenStats(
            ingested=ingested,
            failed=failed,
            moved=move_stats.moved,
            deduped=move_stats.deduped,
            reconciled=(reconcile_stats.inserted if reconcile_stats else 0),
            resolved=resolved_count,
            distilled=distilled,
            consolidated=consolidated,
            gaps_discovered=gaps_discovered,
        )
        logger.info(
            "knowledge.garden: resolved=%d moved=%d deduped=%d reconciled=%d ingested=%d failed=%d distilled=%d consolidated=%d gaps_discovered=%d",
            stats.resolved,
            stats.moved,
            stats.deduped,
            stats.reconciled,
            stats.ingested,
            stats.failed,
            stats.distilled,
            stats.consolidated,
            stats.gaps_discovered,
        )
        return stats

    def _consolidate_task_views(self) -> int:
        """Generate daily and weekly task rollup notes in _processed/.

        Returns the number of files written.
        """
        if self.session is None:
            return 0

        from knowledge.store import KnowledgeStore

        store = KnowledgeStore(self.session)
        today = date.today()
        today_iso = today.isoformat()
        # ISO week: 2026-W16
        week_str = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"

        _SIZE_ORDER = {"small": 0, "medium": 1, "large": 2, "unknown": 3}

        def _size_sort_key(task: dict) -> int:
            s = task.get("size")
            if s is None:
                return 4
            return _SIZE_ORDER.get(s, 4)

        def _size_summary(tasks: list[dict]) -> str:
            counts: dict[str, int] = {}
            for t in tasks:
                s = t.get("size")
                if s is not None:
                    counts[s] = counts.get(s, 0) + 1
            parts = []
            for label in ["small", "medium", "large", "unknown"]:
                if label in counts:
                    suffix = " (review these)" if label == "unknown" else ""
                    parts.append(f"{counts[label]} {label}{suffix}")
            return ", ".join(parts) if parts else "none"

        def _format_task_line(task: dict, *, show_due: bool = True) -> str:
            note_id = task["note_id"]
            title = task["title"]
            size = task.get("size")
            due = task.get("due")
            status = task.get("status", "")
            blocked_by = task.get("blocked_by", [])

            size_str = f"{size}" if size else ""
            parts = [p for p in [size_str] if p]
            if show_due and due:
                parts.append(f"due {due}")
            meta = ", ".join(parts)
            meta_str = f" ({meta})" if meta else ""

            suffix = ""
            if status == "blocked" and blocked_by:
                blockers = ", ".join(blocked_by)
                suffix = f" \U0001f512 blocked by {blockers}"
            elif due and due < today_iso:
                suffix = " \u26a0\ufe0f overdue"

            return f"- [ ] **{note_id}** \u2014 {title}{meta_str}{suffix}"

        # --- Fetch all tasks with due dates ---
        all_tasks = store.list_tasks(include_someday=False)
        tasks_with_due = [t for t in all_tasks if t.get("due") is not None]

        written = 0
        self.processed_root.mkdir(parents=True, exist_ok=True)

        # --- Daily note: due today or overdue ---
        daily_tasks = [t for t in tasks_with_due if t["due"] <= today_iso]
        daily_tasks.sort(key=_size_sort_key)

        daily_lines = [
            "---",
            f"id: tasks-daily-{today_iso}",
            f'title: "Daily Tasks \u2014 {today_iso}"',
            "type: fact",
            "tags: [tasks, daily]",
            "---",
            "",
            "## Due Today / Overdue",
            "",
        ]
        for t in daily_tasks:
            daily_lines.append(_format_task_line(t))
        daily_lines.append("")
        daily_lines.append(f"**Summary:** {_size_summary(daily_tasks)}")
        daily_lines.append("")

        daily_path = self.processed_root / f"tasks-daily-{today_iso}.md"
        daily_path.write_text("\n".join(daily_lines))
        written += 1

        # --- Weekly note: tasks due this week grouped by day ---
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        monday_iso = monday.isoformat()
        sunday_iso = sunday.isoformat()

        weekly_tasks = [
            t for t in tasks_with_due if monday_iso <= t["due"] <= sunday_iso
        ]
        # Also include overdue tasks (before monday)
        overdue_tasks = [t for t in tasks_with_due if t["due"] < monday_iso]
        all_weekly = overdue_tasks + weekly_tasks

        # Group by due date
        by_day: dict[str, list[dict]] = {}
        for t in all_weekly:
            by_day.setdefault(t["due"], []).append(t)

        weekly_lines = [
            "---",
            f"id: tasks-weekly-{week_str}",
            f'title: "Weekly Tasks \u2014 {week_str}"',
            "type: fact",
            "tags: [tasks, weekly]",
            "---",
            "",
        ]

        day_names = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        for day_str in sorted(by_day.keys()):
            d = date.fromisoformat(day_str)
            day_name = day_names[d.weekday()]
            weekly_lines.append(f"## {day_str} ({day_name})")
            weekly_lines.append("")
            day_tasks = sorted(by_day[day_str], key=_size_sort_key)
            for t in day_tasks:
                weekly_lines.append(_format_task_line(t, show_due=False))
            weekly_lines.append("")

        weekly_lines.append(f"**Summary:** {_size_summary(all_weekly)}")
        weekly_lines.append("")

        weekly_path = self.processed_root / f"tasks-weekly-{week_str}.md"
        weekly_path.write_text("\n".join(weekly_lines))
        written += 1

        return written

    def _discover_gaps(self) -> int:
        """Discover unresolved wikilinks for the current cycle.

        Returns the discovered_count for this cycle.

        Classification is owned by the ``knowledge.classify-gaps`` scheduled
        job (registered in ``service.py``); the gardener no longer routes
        gaps through ``classify_gaps`` itself.

        Errors are caught and logged; the gardener cycle must not fail
        because of gap-pipeline bugs.
        """
        if self.session is None:
            return 0
        try:
            # Imported at call-time to avoid a circular import:
            # ``knowledge.gaps`` imports ``_slugify`` from this module.
            from knowledge.gaps import discover_gaps

            return discover_gaps(self.session, self.vault_root)
        except Exception:
            logger.exception("gardener: discover_gaps failed (non-fatal)")
            return 0

    async def _distill_completed_tasks(self) -> tuple[int, int]:
        """Distill learnings from completed tasks into knowledge atoms.

        Returns (distilled, failed) counts.
        """
        if self.session is None:
            return 0, 0

        # Query all active-type notes, then filter for done status in Python
        # (avoids JSONB dialect differences between Postgres and SQLite).
        active_notes = self.session.exec(
            select(Note).where(Note.type == "active")
        ).all()
        done_tasks = [
            n
            for n in active_notes
            if isinstance(n.extra, dict) and n.extra.get("status") == "done"
        ]

        distilled = 0
        failed = 0

        for note in done_tasks:
            # Check if already distilled (provenance exists for this note+version)
            existing = self.session.exec(
                select(AtomRawProvenance).where(
                    and_(
                        AtomRawProvenance.atom_fk == note.id,
                        AtomRawProvenance.gardener_version == GARDENER_VERSION,
                    )
                )
            ).first()
            if existing is not None:
                continue

            # Read the vault file
            vault_path = self.vault_root / note.path
            if not vault_path.is_file():
                continue

            try:
                await self._distill_one(note, vault_path)
                distilled += 1
            except Exception:
                logger.exception("Distillation failed for %s", note.note_id)
                failed += 1

        return distilled, failed

    async def _distill_one(self, note: Note, vault_path: Path) -> None:
        """Distill learnings from a single completed task."""
        prompt = _DISTILL_PROMPT.format(
            note_id=note.note_id,
            note_path=str(vault_path),
            processed_root=str(self.processed_root),
            title=note.title,
        )

        # Capture files before
        before = (
            set(self.processed_root.glob("*.md"))
            if self.processed_root.is_dir()
            else set()
        )

        await self._run_claude_subprocess(prompt)

        # Capture files after
        after = (
            set(self.processed_root.glob("*.md"))
            if self.processed_root.is_dir()
            else set()
        )
        new_files = after - before

        # Record provenance for each new note
        if self.session is not None:
            for f in new_files:
                meta, _ = _split_frontmatter(f.read_text())
                derived_id = meta.get("id", f.stem)
                self.session.add(
                    AtomRawProvenance(
                        atom_fk=note.id,
                        gardener_version=GARDENER_VERSION,
                        derived_note_id=derived_id,
                    )
                )

            # If no new notes, still record provenance to avoid re-distilling
            if not new_files:
                self.session.add(
                    AtomRawProvenance(
                        atom_fk=note.id,
                        gardener_version=GARDENER_VERSION,
                        derived_note_id="no-new-notes",
                    )
                )

            self.session.commit()

    def _discover_raw_files(self) -> list[Path]:
        """Find .md files in the vault root that are not in excluded directories."""
        raw: list[Path] = []
        if not self.vault_root.exists():
            return raw
        for entry in self.vault_root.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.name in _EXCLUDED_DIRS:
                continue
            if entry.is_file():
                if entry.suffix == ".md":
                    raw.append(entry)
                continue
            if entry.is_dir():
                for p in entry.rglob("*.md"):
                    rel = p.relative_to(self.vault_root)
                    if any(part.startswith(".") for part in rel.parts):
                        continue
                    raw.append(p)
        return sorted(raw)

    async def _run_claude_subprocess(self, prompt: str) -> None:
        """Spawn a claude Code subprocess and wait for completion."""
        proc = await asyncio.create_subprocess_exec(
            self.claude_bin,
            "--print",
            "--dangerously-skip-permissions",
            "--allowedTools",
            "Bash,Read,Write,Edit,WebSearch,WebFetch",
            "-p",
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.vault_root,
            # HOME=/ in the container (non-root uid 65532) is not writable, so
            # claude cannot create ~/.claude/ and exits silently with code 0.
            # Override HOME to /tmp which is always writable.
            env={**os.environ, "HOME": "/tmp"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_CLAUDE_TIMEOUT_SECS
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"claude timed out after {_CLAUDE_TIMEOUT_SECS}s")

        if proc.returncode != 0:
            raise RuntimeError(
                f"claude exited {proc.returncode}: "
                f"{stderr.decode(errors='replace')[:300]}"
            )

        self._last_stdout = stdout

    def _record_failed_provenance(self, raw_row: RawInput, exc: Exception) -> None:
        """Record or update a failed provenance row for a raw that failed decomposition."""
        if self.session is None:
            return
        existing = self.session.exec(
            select(AtomRawProvenance).where(
                and_(
                    AtomRawProvenance.raw_fk == raw_row.id,
                    AtomRawProvenance.derived_note_id == "failed",
                )
            )
        ).first()
        if existing is not None:
            existing.retry_count += 1
            existing.error = str(exc)[:500]
            existing.gardener_version = GARDENER_VERSION
            self.session.add(existing)
        else:
            self.session.add(
                AtomRawProvenance(
                    raw_fk=raw_row.id,
                    derived_note_id="failed",
                    gardener_version=GARDENER_VERSION,
                    error=str(exc)[:500],
                    retry_count=1,
                )
            )
        self.session.commit()

    @staticmethod
    def _research_source_tier(raw_meta: dict) -> str | None:
        """Return the source_tier label for a research raw, or None for non-research raws.

        Tier rules (per design doc):
          - 0 successfully-fetched web_fetch sources → "personal" (vault-grounded only)
          - 1 successfully-fetched web_fetch source  → "direct"   (single primary source)
          - 2+ successfully-fetched web_fetch sources → "research" (cross-source synthesis)

        Only ``web_fetch`` entries with a truthy ``url`` count — a skipped fetch
        recorded as ``{tool: web_fetch, url: null}`` does not contribute.
        """
        if raw_meta.get("type") != "research":
            return None
        sources = raw_meta.get("sources") or []
        if not isinstance(sources, list):
            return "personal"
        web_fetch_count = sum(
            1
            for s in sources
            if isinstance(s, dict) and s.get("tool") == "web_fetch" and s.get("url")
        )
        if web_fetch_count == 0:
            return "personal"
        if web_fetch_count == 1:
            return "direct"
        return "research"

    @staticmethod
    def _project_source_tier_onto_atom(atom_path: Path, tier: str) -> None:
        """Inject ``source_tier: <tier>`` into the atom file's YAML frontmatter.

        Idempotent: skips the rewrite when the value already matches. Silently
        skips files that lack a frontmatter block or whose YAML cannot be
        parsed — those are outside the gardener's contract for this projection.
        """
        try:
            text = atom_path.read_text(encoding="utf-8")
        except OSError:
            return
        if not text.startswith("---\n"):
            return
        parts = text.split("---\n", 2)
        if len(parts) < 3:
            return
        try:
            fm = yaml.safe_load(frontmatter._sanitize_yaml_block(parts[1]))
        except yaml.YAMLError:
            return
        if not isinstance(fm, dict):
            return
        if fm.get("source_tier") == tier:
            return
        fm["source_tier"] = tier
        new_block = yaml.dump(fm, default_flow_style=False, sort_keys=False)
        atom_path.write_text(f"---\n{new_block}---\n{parts[2]}", encoding="utf-8")

    async def _ingest_one(self, path: Path) -> None:
        """Decompose a single raw note by spawning a claude Code subprocess."""
        raw_text = path.read_text(encoding="utf-8")
        meta, body = frontmatter.parse(raw_text)
        title = meta.title or path.stem
        # Read the raw frontmatter as a plain dict so the source_tier projector
        # can inspect ``type`` and ``sources`` without going through the typed
        # ``ParsedFrontmatter`` dataclass (which doesn't expose ``sources``).
        raw_meta_dict, _ = _split_frontmatter(raw_text)

        # Look up RawInput row for raw_id breadcrumb and provenance.
        raw_row: RawInput | None = None
        raw_id = ""
        if self.session is not None:
            raw_row = self.session.exec(
                select(RawInput).where(
                    RawInput.path == str(path.relative_to(self.vault_root))
                )
            ).first()
            if raw_row is not None:
                raw_id = raw_row.raw_id

        prompt = _CLAUDE_PROMPT_HEADER.format(
            processed_root=self.processed_root,
            title=title,
            raw_id=raw_id,
            raw_file_path=path,
        )

        before = (
            set(self.processed_root.glob("*.md"))
            if self.processed_root.exists()
            else set()
        )

        self._last_stdout = b""
        try:
            await self._run_claude_subprocess(prompt)
        except Exception as exc:
            if raw_row is not None:
                self._record_failed_provenance(raw_row, exc)
            raise

        after = (
            set(self.processed_root.glob("*.md"))
            if self.processed_root.exists()
            else set()
        )
        new_files = sorted(after - before)

        # Project source_tier onto each new atom file derived from a research
        # raw. Runs before provenance recording so the on-disk frontmatter is
        # consistent with the raw_fk that links these atoms back to the raw.
        tier = self._research_source_tier(raw_meta_dict)
        if tier is not None:
            for new_file in new_files:
                self._project_source_tier_onto_atom(new_file, tier)

        if not new_files:
            logger.warning(
                "gardener: claude produced no notes for %s; leaving raw file in place\n"
                "  stdout: %s",
                path,
                self._last_stdout.decode(errors="replace")[:500],
            )
            # Record a sentinel so this raw is not reprocessed every cycle.
            if raw_row is not None and self.session is not None:
                self.session.add(
                    AtomRawProvenance(
                        raw_fk=raw_row.id,
                        derived_note_id="no-new-notes",
                        gardener_version=GARDENER_VERSION,
                    )
                )
                self.session.commit()
            return

        if raw_row is not None and self.session is not None:
            for new_file in new_files:
                try:
                    file_meta, _ = frontmatter.parse(
                        new_file.read_text(encoding="utf-8")
                    )
                    note_id = file_meta.note_id
                except Exception:
                    logger.debug(
                        "gardener: failed to parse frontmatter for %s, skipping",
                        new_file,
                    )
                    continue
                if not note_id:
                    continue
                self.session.add(
                    AtomRawProvenance(
                        raw_fk=raw_row.id,
                        derived_note_id=note_id,
                        gardener_version=GARDENER_VERSION,
                    )
                )
            self.session.commit()
