"""Database model for the private product-update journal."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

_JSONB = JSONB().with_variant(JSON(), "sqlite")


class ProductUpdate(SQLModel, table=True):
    __tablename__ = "product_update"
    __table_args__ = (
        UniqueConstraint("source_head_sha", name="product_update_head_sha_key"),
        {"schema": "updates", "extend_existing": True},
    )

    published_on: date = Field(primary_key=True)
    category: str
    headline: str
    summary: str
    highlights: list[dict[str, str]] = Field(sa_column=Column(_JSONB, nullable=False))
    improvements: list[dict[str, str]] = Field(
        default_factory=list, sa_column=Column(_JSONB, nullable=False)
    )
    projects: list[str] = Field(sa_column=Column(_JSONB, nullable=False))
    technologies: list[str] = Field(
        default_factory=list, sa_column=Column(_JSONB, nullable=False)
    )
    source_base_sha: str
    source_head_sha: str
    source_commit_count: int
    submitted_by: str
    submitted_authority: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
