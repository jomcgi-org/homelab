"""SQLModel definitions for the faas schema (EmberVM zip-lane function registry).

Mirrors chart/migrations/20260714000000_faas_function.sql. Postgres is the
single source of truth: one row per function name (global uniqueness is the
PK, ADR 045). last_smoke_at doubles as the visibility gate: NULL means
registered but not yet smoke-passed (not visible); a set value means the
current zip passed its test-run gate (Task 10) and is servable (Task 11).
"""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class Function(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "function"
    __table_args__ = {"schema": "faas", "extend_existing": True}

    name: str = Field(primary_key=True)
    visibility: str
    runtime: str
    handler: str
    zip_sha256: str
    code_uri: str
    created_by: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_smoke_at: datetime | None = Field(default=None)
