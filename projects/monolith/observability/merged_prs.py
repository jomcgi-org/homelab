"""Merged pull request snapshot model and classification helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel

MERGE_TYPES = (
    "feat",
    "fix",
    "docs",
    "chore",
    "test",
    "refactor",
    "ci",
    "build",
    "perf",
    "style",
    "revert",
    "wip",
)
_TITLE_PREFIX_RE = re.compile(r"^\[[^]]+\]\s+")
_TITLE_RE = re.compile(rf"^({'|'.join(MERGE_TYPES)})(?:\(([^)]+)\))?!?:\s+.+$")
_CLAUDE_CODE_MARKER_RE = re.compile(r"generated with \[claude code\]", re.IGNORECASE)
_CLAUDE_CODE_LINK_RE = re.compile(r"claude\.ai/code", re.IGNORECASE)
_CODEX_FOOTER_RE = re.compile(
    r"^generated with[^\r\n]*codex", re.IGNORECASE | re.MULTILINE
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MergedPR(SQLModel, table=True):
    __tablename__ = "merged_prs"
    __table_args__ = {"schema": "observability", "extend_existing": True}

    number: int = Field(primary_key=True)
    title: str
    merged_at: datetime = Field(sa_type=DateTime(timezone=True))
    additions: int
    deletions: int
    changed_files: int
    type: str
    scope: str | None = None
    agent_authored: bool
    snapshotted_at: datetime = Field(
        default_factory=_utc_now,
        sa_type=DateTime(timezone=True),
    )


def parse_title(title: str) -> tuple[str, str | None]:
    """Return the supported Conventional Commit type and optional scope."""
    unprefixed = _TITLE_PREFIX_RE.sub("", title, count=1)
    match = _TITLE_RE.fullmatch(unprefixed)
    if match is None:
        return ("other", None)
    return (match.group(1), match.group(2))


def is_agent_authored(body: str) -> bool:
    """Return whether a pull request body contains a known agent footer."""
    return bool(
        _CLAUDE_CODE_MARKER_RE.search(body)
        or _CLAUDE_CODE_LINK_RE.search(body)
        or _CODEX_FOOTER_RE.search(body)
    )
