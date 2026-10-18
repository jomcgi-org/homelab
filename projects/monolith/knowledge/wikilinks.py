"""Strip generated Links sections before indexing authored note content."""

from __future__ import annotations

import re

_LINKS_RE = re.compile(r"\n## Links\n.*", re.DOTALL)


def strip_links_section(body: str) -> str:
    """Return body with any ## Links section removed.

    Indexing uses the authored body for chunking and link extraction.
    """
    return _LINKS_RE.sub("", body)
