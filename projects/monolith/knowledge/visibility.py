"""Public-body wikilink stripping for the public_api view boundary."""

from __future__ import annotations

import re
from typing import Iterable

# A wikilink is a [[target]] possibly containing a |alias suffix that
# we ignore for resolution. We resolve to a slug by lowercasing and
# normalising spaces — same convention as gardener / gap_classifier.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n|]+)(?:\|[^\[\]\n]+)?\]\]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.strip().lower()).strip("-")


def strip_private_wikilinks(body: str, public_note_ids: Iterable[str]) -> str:
    """Strip every wikilink whose target is NOT a known public note.

    The public service cannot enumerate private notes (it reads only the
    public_api views, the confidentiality boundary). Keep wikilinks that
    resolve to a public note, strip everything else (private targets and
    dangling links to non-existent notes) to plain display text.
    """
    public = {_slugify(nid) for nid in public_note_ids}

    def _replace(match: "re.Match[str]") -> str:
        display = match.group(1)
        if _slugify(display) in public:
            return match.group(0)
        return display

    return _WIKILINK_RE.sub(_replace, body)
