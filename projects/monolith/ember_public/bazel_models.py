"""SQLModel definition for the bazel_query_savings table (bazel skyframe demo).

Mirrors chart/migrations/20260719000000_bazel_query_savings.sql and
ember_public/models.py's DemoPgSavings. Single-row table: id is fixed to 1,
so there is exactly one all-time counter shared by every visitor of the
/ember/bazel exhibit.

Unlike demo_pg_savings, there is no state-machine credit rule here: every
successful query already knows exactly how much cold-analysis time it
skipped (COLD_ANALYSIS_S minus that run's wall_ms), so accrual is a direct
add on each POST /query response, not a polled banked-to-banked delta.
"""

from sqlmodel import Field, SQLModel


class BazelQuerySavings(SQLModel, table=True):  # nosemgrep
    __tablename__ = "bazel_query_savings"
    __table_args__ = {"extend_existing": True}

    id: int = Field(default=1, primary_key=True)
    total_analysis_s_saved: float = Field(default=0.0)
