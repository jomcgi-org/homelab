"""SQLModel definition for the ember synthetic probe latch."""

from datetime import datetime

from sqlmodel import Field, SQLModel


class EmberSyntheticProbe(SQLModel, table=True):  # nosemgrep
    __tablename__ = "ember_synthetic_probe"
    __table_args__ = {"extend_existing": True}

    demo: str = Field(primary_key=True)
    ok: bool
    detail: str = ""
    latency_ms: float | None = None
    checked_at: datetime
    last_ok_at: datetime | None = None
