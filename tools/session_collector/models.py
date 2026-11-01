"""Shared data structures and helpers for transcript adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    usage: dict | None = None
    models: list[str] = field(default_factory=list)


def token_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records
