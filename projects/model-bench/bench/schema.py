from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskClass(str, Enum):
    MECHANICAL = "mechanical"
    CONFIG_PLUMBING = "config-plumbing"
    CODE_FIX = "code-fix"
    FREE_TEXT = "free-text"


class VerifierSpec(BaseModel):
    kind: str
    args: dict[str, Any] = Field(default_factory=dict)


class AgentConfig(BaseModel):
    """Per-cell budget for an agentic (tool-calling) task.

    max_turns caps the tool-use loop; max_tokens caps a single completion. A null
    max_tokens falls back to the model's own params.max_tokens so a task need not
    restate it. These values feed the cache key so bumping a budget re-runs the cell.
    """

    max_turns: int = 20
    max_tokens: int | None = None


class ModelParams(BaseModel):
    temperature: float = 0.0
    max_tokens: int = 8192


class ModelSpec(BaseModel):
    id: str
    status: Literal["active", "experimental", "retired"] = "active"
    params: ModelParams = Field(default_factory=ModelParams)
    role: Literal["candidate", "anchor"] = "candidate"
    retired_reason: str | None = None
    retired_date: str | None = None


class TaskSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    version: str
    task_class: TaskClass = Field(alias="class")
    # Difficulty tier. easy + standard form the qualification FLOOR (a model must pass
    # them to be a viable candidate); hard tasks differentiate the qualified. See
    # TIER_WEIGHTS / the gate scoring in the report.
    tier: Literal["easy", "standard", "hard"] = "standard"
    mode: Literal["single-shot", "agentic"] = "single-shot"
    prompt: str
    target_files: list[str] = Field(default_factory=list)
    verifier: VerifierSpec
    agent: AgentConfig = Field(default_factory=AgentConfig)
    source_commit: str | None = None


class Attempt(BaseModel):
    passed: bool
    feedback: str
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int


class ResultCell(BaseModel):
    task_id: str
    task_version: str
    model_id: str
    content_hash: str
    outcome: Literal["pass@1", "pass@2", "fail"]
    attempts: list[Attempt]
    cost_usd: float
    harness_version: str
    prompt_template_hash: str
    # Agentic-only signals (None for single-shot cells).
    turns: int | None = None
    tool_use_ok: bool | None = None

    @property
    def total_latency_ms(self) -> int:
        return sum(a.latency_ms for a in self.attempts)

    @property
    def total_tokens(self) -> int:
        return sum(a.prompt_tokens + a.completion_tokens for a in self.attempts)

    @property
    def first_attempt_passed(self) -> bool:
        return self.attempts[0].passed
