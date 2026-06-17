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

# Re-export VISIBILITY_CRITERIA from the canonical profile module so existing
# consumers (gardener.py:161, 197 inline {VISIBILITY_CRITERIA};
# gardener_distill_test.py:30 asserts it in rendered prompts) keep importing
# from knowledge.visibility without modification. The rubric itself lives
# once, in profile.py.
from knowledge.profile import VISIBILITY_CRITERIA  # noqa: F401 (re-export)

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


def strip_private_wikilinks(body: str, public_note_ids: Iterable[str]) -> str:
    """Strip every wikilink whose target is NOT a known public note.

    The public service cannot enumerate private notes (it reads only the
    public_api views), so this inverts sanitize_public_body: keep wikilinks
    that resolve to a public note, strip everything else (private targets and
    dangling links to non-existent notes) to plain display text.
    """
    public = {_slugify(nid) for nid in public_note_ids}

    def _replace(match: "re.Match[str]") -> str:
        display = match.group(1)
        if _slugify(display) in public:
            return match.group(0)
        return display

    return _WIKILINK_RE.sub(_replace, body)


def public_notes_filter() -> ColumnElement[bool]:
    """SQLAlchemy clause selecting only public notes.

    Use ``Note.visibility == 'public'`` rather than ``COALESCE(..., 'private')``
    so the partial index ``notes_visibility_idx`` is selected by the planner.
    """
    return Note.visibility == "public"
