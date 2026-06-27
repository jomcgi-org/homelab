"""Operations over ``claude_agent.agent_base_snapshots`` - the per-repo warm-base
registry (ADR 022, Phase 4).

A warm base is a microVM booted from a repo's env image with main checked out and
the harness warmed, snapshotted once; new threads restore from it for an instant
ready start. Desired-vs-actual: ``request_rebuild`` bumps ``requested_sha`` when a
repo's main advances, and the controller (fc-agentd) rebuilds the base and writes
``built_sha`` back. This module is the read surface plus the rebuild-request
write; the controller owns the build.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from app.db import get_engine

_ROW_COLUMNS = (
    "base_ref",
    "repo",
    "arch",
    "node",
    "requested_sha",
    "built_sha",
    "size_bytes",
    "created_at",
    "built_at",
)

_SELECT = (
    "SELECT base_ref, repo, arch, node, requested_sha, built_sha, size_bytes, "
    "created_at, built_at FROM claude_agent.agent_base_snapshots"
)


def _row_to_dict(row: Any) -> dict:
    return {col: getattr(row, col) for col in _ROW_COLUMNS}


def _base_ref(repo: str, arch: str) -> str:
    """Deterministic base key (one per repo+arch). Sanitised for use as a path."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", f"{repo}-{arch}").strip("-").lower()
    return f"base-{slug}"


def list_bases() -> list[dict]:
    """Return all warm-base rows, repo+arch ordered."""
    sql = text(_SELECT + " ORDER BY repo, arch")
    with Session(get_engine()) as session:
        rows = session.execute(sql).fetchall()
    return [_row_to_dict(r) for r in rows]


def request_rebuild(repo: str, arch: str, main_sha: str) -> dict:
    """Request a base be (re)built at ``main_sha``.

    Upserts the (repo, arch) row, setting ``requested_sha``. The controller
    rebuilds when ``built_sha`` differs from ``requested_sha``. Idempotent: a
    repeated request at the same sha is a no-op for the controller.
    """
    base_ref = _base_ref(repo, arch)
    sql = text(
        """
        INSERT INTO claude_agent.agent_base_snapshots (base_ref, repo, arch, requested_sha)
        VALUES (:base_ref, :repo, :arch, :sha)
        ON CONFLICT (repo, arch)
        DO UPDATE SET requested_sha = EXCLUDED.requested_sha
        RETURNING base_ref, built_sha
        """
    )
    with Session(get_engine()) as session:
        row = session.execute(
            sql, {"base_ref": base_ref, "repo": repo, "arch": arch, "sha": main_sha}
        ).fetchone()
        session.commit()
    return {
        "base_ref": row.base_ref,
        "requested_sha": main_sha,
        "built_sha": row.built_sha,
        "rebuild_pending": row.built_sha != main_sha,
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def serialize(row: dict) -> dict:
    out = dict(row)
    out["created_at"] = _iso(row.get("created_at"))
    out["built_at"] = _iso(row.get("built_at"))
    return out
