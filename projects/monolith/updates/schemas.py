"""Strict input and output contracts for private product updates."""

from __future__ import annotations

import enum
import re
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class UpdateCategory(enum.StrEnum):
    NEW_FEATURE = "new-feature"
    IMPROVEMENT = "improvement"
    FIX = "fix"


class Project(enum.StrEnum):
    DESIGN_SYSTEM = "design-system"
    EMBERVM = "embervm"
    FIRECRACKER = "firecracker"
    HOME_CLUSTER = "home-cluster"
    INFERENCE = "inference"
    MCP = "mcp"
    MODEL_BENCH = "model-bench"
    MONOLITH = "monolith"
    MONOLITH_PUBLIC = "monolith-public"
    OPERATORS = "operators"
    PLATFORM = "platform"
    SEXTANT = "sextant"
    SHARED = "shared"


class Technology(enum.StrEnum):
    AGENTS = "agents"
    CI = "ci"
    DATA = "data"
    DEVELOPER_TOOLS = "developer-tools"
    FRONTEND = "frontend"
    INFERENCE = "inference"
    KUBERNETES = "kubernetes"
    NETWORKING = "networking"
    OBSERVABILITY = "observability"
    SECURITY = "security"
    STORAGE = "storage"


class UpdateItem(BaseModel):
    """One concrete capability or supporting improvement."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=400)


class ProductUpdateSubmission(BaseModel):
    """The complete, immediately visible daily update supplied by an agent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    published_on: date
    category: UpdateCategory
    headline: str = Field(min_length=1, max_length=140)
    summary: str = Field(min_length=1, max_length=600)
    highlights: list[UpdateItem] = Field(min_length=1, max_length=8)
    improvements: list[UpdateItem] = Field(default_factory=list, max_length=8)
    projects: list[Project] = Field(min_length=1, max_length=8)
    technologies: list[Technology] = Field(default_factory=list, max_length=8)
    source_base_sha: str
    source_head_sha: str
    source_commit_count: int = Field(ge=1, le=1000)

    @field_validator("source_base_sha", "source_head_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        normalized = value.lower()
        if not SHA_RE.fullmatch(normalized):
            raise ValueError("must be a full 40-character hexadecimal commit SHA")
        return normalized

    @field_validator("projects", "technologies")
    @classmethod
    def values_must_be_unique(cls, values: list[enum.StrEnum]) -> list[enum.StrEnum]:
        if len(values) != len(set(values)):
            raise ValueError("values must be unique")
        return values

    @field_validator("source_head_sha")
    @classmethod
    def range_must_advance(cls, value: str, info) -> str:
        if info.data.get("source_base_sha") == value:
            raise ValueError("must differ from source_base_sha")
        return value


class ProductUpdateView(BaseModel):
    """Serialized release-journal entry returned to the private frontend."""

    published_on: date
    category: UpdateCategory
    headline: str
    summary: str
    highlights: list[UpdateItem]
    improvements: list[UpdateItem]
    projects: list[Project]
    technologies: list[Technology]
    source_base_sha: str
    source_head_sha: str
    source_commit_count: int
    source_compare_url: str
    submitted_by: str
    created_at: datetime


class FacetCount(BaseModel):
    value: str
    count: int


class ProductUpdateArchive(BaseModel):
    updates: list[ProductUpdateView]
    projects: list[FacetCount]
    technologies: list[FacetCount]
