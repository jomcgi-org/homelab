"""SQLModel definition for the pod restart observation latch."""

from datetime import datetime

from sqlmodel import Field, SQLModel


class PodRestartWatch(SQLModel, table=True):  # nosemgrep
    __tablename__ = "pod_restart_watch"
    __table_args__ = {"extend_existing": True}

    namespace: str = Field(primary_key=True)
    pod: str = Field(primary_key=True)
    container: str = Field(primary_key=True)
    restart_count: int
    last_reason: str | None = None
    observed_at: datetime
