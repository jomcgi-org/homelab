"""Fail-closed visibility decisions and public-body wikilink sanitisation."""

from __future__ import annotations

import re
from typing import Iterable, Literal

from knowledge.models import Note

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
