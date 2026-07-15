"""SQLModel definition for the sandbox.session table (EmberVM R2, ADR embervm/001).

Mirrors chart/migrations/20260715010000_sandbox_session.sql. Postgres is the
single source of truth: one row per caller handle, mapping it to an EmberVM
session id and its per-session capability token so sessioned run_python reuses
the same session across turns.

token is a SECRET (the capability that authenticates every invoke). It is never
logged and this table is private-tier only; no public_reader GRANT exists.

expires_at is the EmberVM session's max-lifetime deadline supplied by the create
API (not a server-generated insert timestamp), so it has no default_factory: it
is NULL until the API value is stored. The class-level nosemgrep covers the
sqlmodel-datetime-without-factory rule for that intentional nullable field.
"""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class SandboxSession(SQLModel, table=True):  # nosemgrep
    __tablename__ = "session"
    __table_args__ = {"schema": "sandbox", "extend_existing": True}

    handle: str = Field(primary_key=True)
    session_id: str
    # SECRET: EmberVM per-session capability token. Never log it.
    token: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = Field(default=None)
