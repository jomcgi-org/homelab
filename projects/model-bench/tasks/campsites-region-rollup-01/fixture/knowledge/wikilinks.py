"""Generate and sync the ## Links section in Obsidian notes from frontmatter edges."""

from __future__ import annotations

import re

from knowledge.frontmatter import ParsedFrontmatter

_LINKS_RE = re.compile(r"\n## Links\n.*", re.DOTALL)

# Edge types whose targets become Up: links, in priority order.
_UP_EDGE_TYPES = ("derives_from", "refines")

# Remaining edge types rendered as labelled bullet lists.
_LABELLED_EDGE_TYPES: dict[str, str] = {
    "related": "Related",
    "generalizes": "Generalizes",
    "contradicts": "Contradicts",
    "supersedes": "Supersedes",
}


def _wikilink(note_id: str) -> str:
    return f"[[_processed/{note_id}|{note_id}]]"


def render_links_section(meta: ParsedFrontmatter) -> str | None:
    """Return the full ## Links block (leading newline included), or None if empty."""
    lines: list[str] = []

    # Up: — first matching up-type edge wins; fall back to type hub.
    for edge_type in _UP_EDGE_TYPES:
        if edge_type in meta.edges:
            for target in meta.edges[edge_type]:
                lines.append(f"Up: {_wikilink(target)}")
            break
    else:
        if meta.type:
            lines.append(f"Up: [[{meta.type}]]")

    for edge_type, label in _LABELLED_EDGE_TYPES.items():
        if edge_type in meta.edges:
            lines.append(f"{label}:")
            for ref in meta.edges[edge_type]:
                lines.append(f"- {_wikilink(ref)}")

    if not lines:
        return None

    return "\n## Links\n\n" + "\n".join(lines) + "\n"


def strip_links_section(body: str) -> str:
    """Return body with any ## Links section removed.

    Used by the reconciler to obtain the authored body for chunking and
    link extraction, regardless of whether the Links section was already
    written by the pre-sync pass.
    """
    return _LINKS_RE.sub("", body)


def sync_links(raw: str, meta: ParsedFrontmatter) -> str | None:
    """Return updated file text with ## Links synced, or None if already current.

    Strips any existing ## Links section (including everything after it) and
    appends the freshly rendered one. Returns None if the result is identical
    to the input so callers can skip the write.
    """
    expected = render_links_section(meta)
    stripped = _LINKS_RE.sub("", raw).rstrip()
    new_raw = stripped + ("\n" + expected if expected else "\n")
    return None if new_raw == raw else new_raw
