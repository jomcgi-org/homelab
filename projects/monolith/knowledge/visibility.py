"""Visibility helper: criteria, coalescing rule, wikilink sanitiser, SQL filter.

All knowledge-pipeline code that needs to reason about public vs private
content imports from here. Drift between the criteria the LLM sees and
the criteria the routes enforce is the most common way these systems leak.
"""

from __future__ import annotations

import re
from typing import Iterable, Literal

from sqlalchemy.sql import ColumnElement

from knowledge.models import Note

VISIBILITY_CRITERIA = """\
## Visibility (REQUIRED frontmatter field)

Every note MUST set `visibility: public` or `visibility: private`.
This controls whether the note appears on Joe's public website.

Default to `private` whenever you are uncertain.

Mark `public` when the note is about:
- General engineering concepts, principles, heuristics (DORA, Conway's Law,
  blameless postmortems, etc.) — anything you'd find in a textbook, blog,
  or conference talk.
- Skills, technologies, or methods covered in Joe's public CV / GitHub /
  conference talks.
- Verifiable facts about external systems, libraries, protocols, or tools.
- Book / paper / talk summaries when the source is publicly available.

Mark `private` when the note involves any of:
- Names of current or former colleagues, managers, reports, or interviewers.
- Specific employers in non-public ways: project codenames, internal
  architecture, compensation, performance reviews, hiring decisions.
- Job-search activity: interview prep, comp negotiation, target companies,
  reasons-for-leaving, offer comparisons.
- Personal life: family, finances, health, relationships, legal matters,
  living situation.
- Critiques or hot takes about identifiable people or companies that
  aren't already in Joe's public writing.
- Active tasks, daily/weekly journals, blockers — anything operational
  about Joe's current work.

Edge cases:
- An atom about a generally-applicable pattern that includes a
  workplace-specific example: rewrite the example out and mark public,
  OR keep the example and mark private. Do not mark public with the
  example intact.
- A fact about an external library mentioned during a private incident:
  the fact is public, the incident framing is private — split into two
  notes if needed.

When in doubt: `private`.
"""

EffectiveVisibility = Literal["public", "private"]

_VALID = {"public", "private"}

# A wikilink is a [[target]] possibly containing a |alias suffix that
# we ignore for resolution. We resolve to a slug by lowercasing and
# normalising spaces — same convention as gardener / gap_classifier.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n|]+)(?:\|[^\[\]\n]+)?\]\]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def effective_visibility(note: Note | object) -> EffectiveVisibility:
    """Return the public-or-private decision for a note.

    Null and unknown values both fall to ``private`` so a misconfigured
    or malformed value never accidentally exposes content.
    """
    raw = getattr(note, "visibility", None)
    if raw in _VALID:
        return raw  # type: ignore[return-value]
    return "private"


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.strip().lower()).strip("-")


def sanitize_public_body(body: str, private_target_ids: Iterable[str]) -> str:
    """Strip every wikilink whose target is in ``private_target_ids``.

    Strip = replace ``[[Foo Bar]]`` with ``Foo Bar``. Bracket text is not
    redacted — the design accepts that wikilink display text may contain
    weak signals (the loud leak is the *graph edge*, which the public
    graph endpoint already filters out).

    Wikilinks whose target is NOT in ``private_target_ids`` are left
    untouched so the frontend renderer can still resolve them to public
    notes.
    """
    private = {p for p in private_target_ids}

    def _replace(match: re.Match[str]) -> str:
        display = match.group(1)
        slug = _slugify(display)
        if slug in private:
            return display
        return match.group(0)

    return _WIKILINK_RE.sub(_replace, body)


def public_notes_filter() -> ColumnElement[bool]:
    """SQLAlchemy clause selecting only public notes.

    Use ``Note.visibility == 'public'`` rather than ``COALESCE(..., 'private')``
    so the partial index ``notes_visibility_idx`` is selected by the planner.
    """
    return Note.visibility == "public"
