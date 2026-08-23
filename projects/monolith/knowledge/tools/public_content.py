"""Content guard for documents that reach jomcgi.dev.

The public docs and posts pipelines allowlist by path (a fixed project list, a
``public: true`` frontmatter gate). A doc that is correctly public can still
paste an internal identifier into itself, which is how an in-cluster hostname
reached the public site twice on 2026-08-22 (#5144). This is the only content
check and it fails the generator loudly; the generators run in CI's Format
stage, so the failure lands on the PR rather than on the merge-time regen.

The repository itself is public, so the markers target in-cluster addressing
and pasted credentials, not identifiers that already appear in source.
"""

from __future__ import annotations

import re

_INTERNAL_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("in-cluster service hostname", re.compile(r"\.svc\.cluster\.local")),
    ("1Password op:// reference", re.compile(r"op://")),
    ("1Password vault item path", re.compile(r"vaults/[\w-]+/items/")),
    (
        "private IPv4 address",
        re.compile(
            r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)"
            r"(?:\.\d{1,3}){2}\b"
        ),
    ),
    ("cluster node name", re.compile(r"\bnode-\d+\b")),
    ("brick name", re.compile(r"\bbrick-\d+\b")),
    ("S3 bucket URI", re.compile(r"\bs3://")),
    # A hostname ending in .internal (ca.egress.internal). The lookahead keeps
    # dotted YAML key paths such as egress.internal.allowlist out of it.
    (
        ".internal hostname",
        re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.internal\b(?!\.\w)"),
    ),
    # A secret-shaped env var WITH a value pasted next to it. Bare names are
    # already in the public source tree, so only the assignment is a leak.
    (
        "secret env var assignment",
        re.compile(
            r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_"
            r"(?:TOKEN|SECRET|PASSWORD|PASSPHRASE|API_KEY|PRIVATE_KEY)"
            r"[ \t]*[=:][ \t]*\S"
        ),
    ),
)


def check_public_content(rel_path: str, content: str) -> None:
    """Raise ``SystemExit`` if a public document contains an internal marker."""
    for label, pattern in _INTERNAL_MARKERS:
        match = pattern.search(content)
        if match:
            line = content.count("\n", 0, match.start()) + 1
            raise SystemExit(f"{rel_path}:{line}: public doc contains {label}")
