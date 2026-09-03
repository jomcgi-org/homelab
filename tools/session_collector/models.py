"""Shared data structures for transcript adapters."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Block:
    kind: str
    text: str
    name: str | None = None


@dataclass
class Turn:
    blocks: list[Block] = field(default_factory=list)


@dataclass
class Session:
    provider: str
    session_id: str
    cwd: str
    git_branch: str | None
    model: str | None
    started_at: str
    ended_at: str
    title: str
    records_total: int
    records_kept: int
    collector_version: str
    turns: list[Turn]
    git_origin: str | None = None
