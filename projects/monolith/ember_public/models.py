"""SQLModel definitions for the ember demo savings tables.

DemoPgSavings mirrors chart/migrations/20260718000000_demo_pg_savings.sql.
DemoSgSavings mirrors chart/migrations/20260719020000_demo_sg_savings.sql.
Both are single-row tables: id is fixed to 1, so there is exactly one
all-time counter shared by every visitor of the respective exhibit.
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


class DemoSgSavings(SQLModel, table=True):  # nosemgrep
    """Mirrors chart/migrations/20260719020000_demo_sg_savings.sql. Single-row
    table: id is fixed to 1, so there is exactly one all-time counter shared
    by every visitor of the semgrep exhibit.

    Unlike DemoPgSavings, there is no state-machine credit rule here: every
    successful scan already knows exactly how much time it saved versus the
    hosted baseline (semgrep_core.saved_ms), so accrual is a direct add on
    each POST /scan response, mirroring BazelQuerySavings.
    """

    __tablename__ = "demo_sg_savings"
    __table_args__ = {"extend_existing": True}

    id: int = Field(default=1, primary_key=True)
    scans: int = Field(default=0)
    actual_ms: int = Field(default=0)
    saved_ms: int = Field(default=0)
