"""SQLModel definition for the demo_pg_savings table (demo-postgres exhibit).

Mirrors chart/migrations/20260718000000_demo_pg_savings.sql. Single-row
table: id is fixed to 1, so there is exactly one all-time counter shared by
every visitor of the demo-postgres exhibit.
"""

from datetime import datetime

from sqlmodel import Field, SQLModel


class DemoPgSavings(SQLModel, table=True):  # nosemgrep
    __tablename__ = "demo_pg_savings"
    __table_args__ = {"extend_existing": True}

    id: int = Field(default=1, primary_key=True)
    total_mib_seconds: float = Field(default=0.0)
    last_sample_at: datetime | None = Field(default=None)
    last_state: str | None = Field(default=None)
    last_generation: int | None = Field(default=None)
