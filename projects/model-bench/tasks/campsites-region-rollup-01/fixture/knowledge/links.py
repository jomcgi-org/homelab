"""Extract [[wikilinks]] from markdown bodies."""

from __future__ import annotations

import re
from dataclasses import dataclass

_FENCED = re.compile(r"```.*?```", re.DOTALL)
_INLINE = re.compile(r"`[^`\n]*`")
_WIKILINK = re.compile(r"\[\[([^\[\]\n|]+?)(?:\|([^\[\]\n]+?))?\]\]")


@dataclass(frozen=True)
class Link:
    target: str
    display: str | None


def extract(body: str) -> list[Link]:
    stripped = _FENCED.sub("", body)
    stripped = _INLINE.sub("", stripped)
    seen: set[str] = set()
    out: list[Link] = []
    for match in _WIKILINK.finditer(stripped):
        target = match.group(1).strip()
        # Strip heading anchor (#section) or block reference (^id) so that
        # [[Note#Heading]] and [[Note^block]] both resolve to the note name.
        # This ensures graph edges target note_ids, not ephemeral sub-anchors.
        target = re.split(r"[#^]", target, maxsplit=1)[0].strip()
        if not target or target in seen:
            continue
        seen.add(target)
        display = match.group(2).strip() if match.group(2) else None
        out.append(Link(target=target, display=display))
    return out
