"""Visibility drift detector: compare DB visibility column vs file frontmatter.

Background: today's audit found 3225 atoms with null DB visibility despite
earlier bulk classification work. The root cause was MCP edit_note silently
dropping the visibility field on rewrite (fixed in PR #2380). This module
is the defensive read-side counterpart that detects any future drift between
file frontmatter and the DB column.

Why drift can still happen even with the write-side fix:
- Direct file edits (Joe-via-Obsidian, manual cleanup scripts) update the
  file without going through the API
- The reconciler is hash-based and only reprocesses files whose content
  hash changed; in the brief window between an edit and the next
  reconciler tick the DB and file disagree
- Any future MCP/API write tool with a similar field-list bug

The detector logs drift cases and emits a count via the standard logging
path. There is no auto-healing -- a human reviews the warnings and decides
whether to re-run set_note_visibility or repair the file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from knowledge import frontmatter
from knowledge.models import Note
from knowledge.service import DEFAULT_VAULT_ROOT, VAULT_ROOT_ENV

logger = logging.getLogger(__name__)


_MAX_SAMPLE_CASES = 100
_MAX_LOGGED_CASES = 50


@dataclass(frozen=True)
class DriftCase:
    note_id: str
    path: str
    db_visibility: str | None
    file_visibility: str | None


@dataclass(frozen=True)
class DriftStats:
    checked: int
    drift_count: int
    missing_files: int
    parse_failures: int
    drift_cases: tuple[DriftCase, ...] = field(default_factory=tuple)


def detect_visibility_drift(session: Session, vault_root: Path) -> DriftStats:
    """Scan all non-deleted notes and compare DB visibility vs file frontmatter.

    For each note (excluding soft-deleted rows):
    - DB visibility is read from the Note.visibility column
    - File visibility is parsed from the frontmatter at vault_root/note.path
    - A mismatch including the NULL/set asymmetry counts as drift

    Returns a DriftStats with a bounded sample of drift cases. The total
    count covers all drift; the cases tuple is capped at _MAX_SAMPLE_CASES
    so a pathological scenario does not blow up memory or log buffers.
    """
    notes = list(session.scalars(select(Note).where(Note.deleted_at.is_(None))))

    checked = 0
    missing_files = 0
    parse_failures = 0
    total_drift = 0
    sample_cases: list[DriftCase] = []

    for note in notes:
        file_path = vault_root / note.path
        if not file_path.is_file():
            missing_files += 1
            continue

        try:
            raw = file_path.read_text()
            parsed, _body = frontmatter.parse(raw)
        except Exception:
            # Malformed frontmatter is the reconciler's problem to surface;
            # we just count it and move on with a single-line log so the
            # drift sweep does not silently mask a parse regression.
            logger.warning("knowledge.drift_detector: parse failed for %s", note.path)
            parse_failures += 1
            continue

        checked += 1
        if note.visibility != parsed.visibility:
            total_drift += 1
            if len(sample_cases) < _MAX_SAMPLE_CASES:
                sample_cases.append(
                    DriftCase(
                        note_id=note.note_id,
                        path=note.path,
                        db_visibility=note.visibility,
                        file_visibility=parsed.visibility,
                    )
                )

    return DriftStats(
        checked=checked,
        drift_count=total_drift,
        missing_files=missing_files,
        parse_failures=parse_failures,
        drift_cases=tuple(sample_cases),
    )


async def detect_drift_handler(session: Session) -> datetime | None:
    """Scheduler handler: scan for visibility drift and log results.

    Logs a one-line summary plus up to _MAX_LOGGED_CASES detail lines so
    operators can spot regressions. Emits no exception even on partial
    failure -- the next tick retries from scratch.
    """
    vault_root_str = os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT)
    vault_root = Path(vault_root_str)
    if not vault_root.is_dir():
        logger.warning("knowledge.drift_detector: vault root missing: %s", vault_root)
        return None

    stats = detect_visibility_drift(session, vault_root)
    logger.info(
        "knowledge.drift_detector: checked=%d drift=%d missing=%d parse_fail=%d",
        stats.checked,
        stats.drift_count,
        stats.missing_files,
        stats.parse_failures,
    )
    for case in stats.drift_cases[:_MAX_LOGGED_CASES]:
        logger.warning(
            "knowledge.drift_detector: %s db=%s file=%s path=%s",
            case.note_id,
            case.db_visibility,
            case.file_visibility,
            case.path,
        )
    return None


__all__ = [
    "DriftCase",
    "DriftStats",
    "detect_visibility_drift",
    "detect_drift_handler",
]
