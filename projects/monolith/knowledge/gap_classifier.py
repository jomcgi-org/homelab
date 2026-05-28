"""Claude-backed gap classifier.

Spawns a `claude --print` subprocess with a batch of stub paths and a
classification rubric. Claude reads each stub's frontmatter via the Read
tool, decides one of four classes, and uses Edit to update the stub's
frontmatter in place. Allowed tools are restricted to Read and Edit —
the classifier cannot Write new files or Bash new processes.

The reconciler (on its next tick) projects the updated frontmatter into
the Gap row. This module does not touch the DB directly.

Privacy-conservative fallback: if Claude returns an invalid class or
cannot decide, the stub stays `gap_class: null` and gets retried by the
next classifier tick. We do not auto-route to internal just because the
classifier hiccuped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

CLASSIFIER_VERSION = "opus-4-7@v3"

_CLASSIFY_TIMEOUT_SECS = 300  # 5 minutes per batch — generous for N=10 stubs


_CLASSIFIER_PROMPT = """\
You are classifying knowledge graph gaps — unresolved [[wikilinks]] in an
Obsidian vault — into four classes.

## The four classes

- **external**: publicly-researchable concept. Example: "Linkerd mTLS",
  "Rust ownership", "Merkle tree". A web search or public docs would
  find primary sources.
- **internal**: only the user can resolve. Example: their therapist's
  name, a shorthand for a friend, notes about their own childhood or
  work decisions. Searching the web is useless AND a privacy leak.
- **hybrid**: externally researchable but benefits from user annotation.
  Example: "my-config-for-neovim" — the tool exists externally, but the
  user's specific setup is personal.
- **parked**: queryable but not worth current budget. Example: highly
  niche terms, project-specific jargon that may never be revisited.

## Rules

- **Privacy-conservative default:** if uncertain between external/internal,
  choose internal. Over-routing to user is tolerated. Over-routing to web
  is treated as a defect.
- **Use the Read tool** to inspect each stub's frontmatter (id, title,
  referenced_by). The referenced_by list tells you which source notes
  link to this term — that's your context.
- **Use the Edit tool** to update these frontmatter fields. The stub's
  frontmatter already contains all four keys with placeholder values
  (`gap_class: null`, `status: discovered`, `classified_at: null`,
  `classifier_version: null`). For each key, find the existing line and
  replace it — do not add a new line. YAML requires unique top-level
  keys; appending a duplicate key produces an ugly stub even when YAML
  parsers tolerate it. Example:
  - find: `gap_class: null` → replace with one of: `gap_class: external`
    | `gap_class: internal` | `gap_class: hybrid` | `gap_class: parked`
  - find: `status: discovered` → replace with the status that matches
    the class you just chose:
      - `status: in_review` for `gap_class: external`, `internal`, or
        `hybrid` (every gap that could ever flow into the research
        pipeline or need a user answer goes through the review queue
        first — Joe approves external rows from there before any
        tokens are spent)
      - `status: classified` for `gap_class: parked` only (terminal
        — never flows further; classified is the terminal label for
        parked gaps that bypass the review queue)
  - find: `classified_at: null` → replace with the current ISO timestamp
    (UTC, e.g. `classified_at: '2026-04-25T08:00:00Z'`)
  - find: `classifier_version: null` → replace with
    `classifier_version: {classifier_version}`
- **Do not** modify any other field. Do not add a body. Do not write new
  files. Do not run any Bash command.
- If you cannot decide on a class for a stub, skip it (leave gap_class
  null). Do not guess.

## Person-atom sub-classification (when applicable)

When the gap term names a PERSON, additionally tag the resulting atom
with one of these sub-tags so the privacy review can route correctly:

- `person:public` — public historical or contemporary figures: authors,
  scientists, philosophers, public intellectuals, sports figures, etc.
  The atom can safely default to `visibility: public`. Examples:
  Daniel Kahneman, Bertrand Russell, Christopher Alexander, Andy
  Matuschak, Tom Chivers, Pierre-Simon Laplace.
- `person:peer` — friends, family members, colleagues, Discord channel
  members, or anyone Joe has a personal relationship with. The atom
  defaults to `visibility: private`. Examples: anyone whose primary
  reference is a Discord channel snapshot, anyone tagged `person` AND
  `discord`, anyone with `user-profile` or `channel-snapshot` co-tags.

If you cannot confidently distinguish (e.g. an ambiguous mononym, a
historical figure you don't recognise), default to `person:peer` —
privacy-conservative is the right error direction. The reviewer can
upgrade to `person:public` from the review queue.

## Stubs to classify

{stub_list}
"""


@dataclass(frozen=True)
class ClassifyStats:
    stubs_processed: int
    duration_ms: int


async def classify_stubs(
    stubs: list[Path],
    *,
    claude_bin: str = "claude",
) -> ClassifyStats:
    """Classify a batch of gap stubs by spawning a `claude --print` subprocess.

    Claude edits each stub's frontmatter via the Edit tool. The reconciler
    picks up the changes on its next tick and projects them into the DB.

    Returns ClassifyStats with the batch size and wall-clock duration.
    Non-zero exit from the subprocess logs a warning with stderr excerpt
    but does not raise — the next tick retries on the same stubs.
    """
    if not stubs:
        return ClassifyStats(stubs_processed=0, duration_ms=0)

    # All stub paths must be absolute — Claude's Read/Edit tools are
    # cwd-sensitive when given relative paths, and we deliberately don't
    # set cwd on the subprocess. Fail loudly if a future caller passes
    # relative paths rather than silently producing broken classifications.
    relative = [s for s in stubs if not s.is_absolute()]
    if relative:
        raise ValueError(
            f"classify_stubs requires absolute paths, got relative: {relative}"
        )

    stub_list = "\n".join(f"- {stub}" for stub in stubs)
    prompt = _CLASSIFIER_PROMPT.format(
        classifier_version=CLASSIFIER_VERSION,
        stub_list=stub_list,
    )

    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        claude_bin,
        "--print",
        "--dangerously-skip-permissions",
        "--allowedTools",
        "Read,Edit",
        "-p",
        prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # HOME=/ in the container (non-root uid 65532) is not writable, so
        # claude cannot create ~/.claude/ and exits silently with code 0.
        # Override HOME to /tmp which is always writable. Same pattern as
        # gardener.py's _run_claude_subprocess.
        env={**os.environ, "HOME": "/tmp"},
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_CLASSIFY_TIMEOUT_SECS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "gap_classifier: subprocess timed out after %ds on batch of %d stubs",
            _CLASSIFY_TIMEOUT_SECS,
            len(stubs),
        )
        return ClassifyStats(stubs_processed=len(stubs), duration_ms=duration_ms)

    duration_ms = int((time.monotonic() - start) * 1000)

    if proc.returncode != 0:
        logger.warning(
            "gap_classifier: subprocess exit=%d on batch of %d stubs; stderr=%s",
            proc.returncode,
            len(stubs),
            stderr.decode(errors="replace")[:300],
        )

    return ClassifyStats(stubs_processed=len(stubs), duration_ms=duration_ms)
