"""SQLModel tables for the World Cup 2026 qualification tracker (schema 'worldcup').

Mirrors chart/migrations/20260620120000_worldcup_schema.sql.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

_SCHEMA = {"schema": "worldcup", "extend_existing": True}


class Standing(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "standings"
    __table_args__ = _SCHEMA
    team_id: str = Field(primary_key=True)
    name: str
    fifa_code: str
    flag_url: str | None = None
    group_name: str
    mp: int = 0
    w: int = 0
    d: int = 0
    l: int = 0
    pts: int = 0
    gf: int = 0
    ga: int = 0
    gd: int = 0
    updated_at: datetime | None = None


class Fixture(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "fixtures"
    __table_args__ = _SCHEMA
    match_id: str = Field(primary_key=True)
    group_name: str
    matchday: int
    home_id: str
    home_name: str
    home_code: str
    away_id: str
    away_name: str
    away_code: str
    home_score: int | None = None
    away_score: int | None = None
    finished: bool = False
    kickoff: datetime | None = None
    updated_at: datetime | None = None


class Qualification(
    SQLModel, table=True
):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "qualification"
    __table_args__ = _SCHEMA
    team_id: str = Field(primary_key=True)
    fifa_code: str
    prob_qualify: float
    prob_top2: float
    prob_third: float
    status: str = "contention"
    n_sims: int
    computed_at: datetime | None = None


class SwingMatch(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "swing_matches"
    __table_args__ = _SCHEMA
    # Composite key: one swing row per (remaining match, country whose qualify
    # chance it moves). Replaces the old single-focus-team (Scotland) model.
    match_id: str = Field(primary_key=True)
    country_code: str = Field(primary_key=True)
    group_name: str
    home_code: str
    away_code: str
    kickoff: datetime | None = None
    swing: float
    p_qualify_home_win: float
    p_qualify_draw: float
    p_qualify_away_win: float
    is_own_match: bool = False  # match involves country_code
    computed_at: datetime | None = None
