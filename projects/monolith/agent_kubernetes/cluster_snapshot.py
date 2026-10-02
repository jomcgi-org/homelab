"""Read the curated cluster summary without Kubernetes credentials or calls."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from core.db import get_engine
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

_STALE_SECS = 600


def _unavailable() -> dict:
    # Database exceptions can contain credentials, connection URLs, or SQL.
    return {
        "ok": False,
        "error": {
            "code": "unavailable",
            "message": "The cluster snapshot is unavailable. Try again after the next refresh.",
        },
    }


def _read_cluster_snapshot(application: str | None) -> dict:
    """Open and use the agents_writer session entirely in the worker thread."""
    try:
        with Session(get_engine()) as session:
            try:
                row = session.execute(
                    text(
                        "SELECT payload, snapshot_at "
                        "FROM agent_view.cluster_snapshot WHERE id = 1"
                    )
                ).first()
            except SQLAlchemyError:
                session.rollback()
                return _unavailable()
        if row is None:
            return {
                "ok": False,
                "error": {
                    "code": "not_found",
                    "message": "No cluster snapshot has been written yet.",
                },
            }

        payload, snapshot_at = row
        # SQLite returns strings and naive timestamps; Postgres parses both.
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(snapshot_at, str):
            snapshot_at = datetime.fromisoformat(snapshot_at)
        if snapshot_at.tzinfo is None:
            snapshot_at = snapshot_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - snapshot_at).total_seconds()
        if application is not None:
            payload = {
                **payload,
                "applications": [
                    row for row in payload["applications"] if row["name"] == application
                ],
            }
        return {
            "ok": True,
            "snapshot_at": snapshot_at.isoformat(),
            "age_seconds": age_seconds,
            "stale": age_seconds > _STALE_SECS,
            "complete": payload["complete"],
            "snapshot": payload,
        }
    except (SQLAlchemyError, ValueError, TypeError, KeyError):
        return _unavailable()


async def cluster_snapshot(application: str | None = None) -> dict:
    """Read a minutes-old summary of ArgoCD Application sync, health and revisions
    plus unhealthy workloads, refreshed every 2 minutes by the private monolith.
    This is a read-only database lookup, not a live Kubernetes query. Treat
    stale true or complete false as unknown, never as healthy. Pass application
    to keep only that exact name in the applications list; an unknown name gives
    an empty list. Other snapshot fields are unchanged. No row returns not_found;
    database failures return unavailable without connection details.
    """
    return await asyncio.to_thread(_read_cluster_snapshot, application)
