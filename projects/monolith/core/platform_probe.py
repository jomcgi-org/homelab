"""Domain-neutral probe latch: the private/public handoff for platform health.

The tier that can COMPUTE platform health is not the tier that can SERVE it.
Computing CD health needs ArgoCD reads and a GitHub token, which only the
private monolith has; the endpoint UptimeRobot polls is the public one. Two
processes, so the value is handed off through Postgres: the private tier's
leader singleton writes a row, the public tier's health component reads it.

**The general rule this is meant to serve: the public tier must never need
privilege to report health.** It is the less-privileged tier by design (no
cluster access, no secrets, a read-only DB role), so any check that needs
privilege should be computed on the private side and handed off through this
latch rather than by widening what public can reach. CD health is the first
consumer; the ember synthetic probes are the same pattern predating it. Prefer
this over granting the public tier a new capability.

Lives in ``core`` rather than a domain because both sides need it and neither
owns it. Deliberately NOT reusing ``ember_synthetic_probe``: that table already
has the shape and the public_reader grant, but putting CD rows in a table named
``ember_*`` would mislead whoever greps it mid-incident, and the writer would
have to reach across domains to import the model.

Reads FAIL OPEN. A missing table (pre-migration rollout), a missing grant, or a
DB blip all yield "no probe recorded yet" and a healthy component, rather than
503ing the public endpoint. That is deliberate: framework/core.py's own SELECT 1
baseline already 503s on a real DB outage, and a monitoring component that
pages for its own storage being unavailable is worse than no component.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlmodel import Field, Session, SQLModel

from core.db import get_engine

logger = logging.getLogger(__name__)


class PlatformProbe(SQLModel, table=True):  # nosemgrep
    __tablename__ = "platform_probe"
    __table_args__ = {"extend_existing": True}

    name: str = Field(primary_key=True)
    ok: bool
    detail: str = ""
    checked_at: datetime
    last_ok_at: datetime | None = None


def _read_sync(name: str) -> PlatformProbe | None:
    with Session(get_engine()) as session:
        return session.get(PlatformProbe, name)


async def read_probe(name: str) -> PlatformProbe | None:
    """Read one latch row. Any failure is reported as "no row", see module doc."""
    try:  # nosemgrep: no-broad-except-swallow - fail-open is the design, logged here
        return await asyncio.to_thread(_read_sync, name)
    except Exception as exc:  # noqa: BLE001 - missing pre-migration table is expected
        logger.warning("platform probe read failed for %s: %s", name, exc)
        return None


def _write_sync(name: str, ok: bool, detail: str) -> None:
    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        row = session.get(PlatformProbe, name)
        if row is None:
            row = PlatformProbe(
                name=name,
                ok=ok,
                detail=detail,
                checked_at=now,
                last_ok_at=now if ok else None,
            )
            session.add(row)
        else:
            row.ok = ok
            row.detail = detail
            row.checked_at = now
            # last_ok_at is the recovery clock the reader uses to report how
            # long something has been down. Only advance it on success.
            if ok:
                row.last_ok_at = now
        session.commit()


async def write_probe(name: str, ok: bool, detail: str) -> None:
    """Write the latch row. Private tier only (the app role owns the write)."""
    await asyncio.to_thread(_write_sync, name, ok, detail)


def probe_health(name: str, staleness_s: float, advisory: bool = False):
    """Build a health component that reads one latch row.

    Mirrors ember_public.health.synthetic_probe_health, which reads the ember
    latch. Staleness matters as much as the ok flag: a writer that has died
    leaves a stale green row, and reporting that as healthy is the exact
    meta-monitoring gap this component exists to close.

    ``advisory`` means the component reports but never contributes to the 503
    decision. The flag is included on every result so it describes the
    component rather than its current outcome.
    """

    async def check() -> dict:
        def _result(**values) -> dict:
            if advisory:
                values["advisory"] = True
            return values

        row = await read_probe(name)
        if row is None:
            return _result(ok=True, detail="no probe recorded yet")

        now = datetime.now(timezone.utc)

        def _utc(dt: datetime) -> datetime:
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

        if not row.ok:
            detail = row.detail or "not ok"
            if row.last_ok_at is not None:
                down_s = max(0.0, (now - _utc(row.last_ok_at)).total_seconds())
                detail = f"{detail}, down for {down_s / 60:.0f}m"
            return _result(ok=False, detail=detail)

        age_s = (now - _utc(row.checked_at)).total_seconds()
        if age_s > staleness_s:
            return _result(
                ok=False,
                detail=f"last probe was {age_s / 60:.0f}m ago, writer may be dead",
            )
        return _result(ok=True, detail=row.detail or "ok")

    return check
