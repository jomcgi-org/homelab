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
    restate it. exec grants the sandboxed `run` shell tool (for tasks where the model
    must set up its own toolchain, e.g. `go mod tidy` + `go test`); it is off by default
    so the file-only tasks keep their calibrated behaviour. These values feed the cache
    key so bumping a budget re-runs the cell.
    """

    max_turns: int = 20
    max_tokens: int | None = None
    exec: bool = False


class ModelParams(BaseModel):
    temperature: float = 0.0
    max_tokens: int = 8192


class ModelSpec(BaseModel):
    id: str
    status: Literal["active", "experimental", "retired"] = "active"
    params: ModelParams = Field(default_factory=ModelParams)
    role: Literal["candidate", "anchor"] = "candidate"
    # Completion backend. "openrouter" (default) rents the model per-token and records
    # real cost/turns/tokens. "claude-code" runs the model through the local `claude`
    # CLI under the Max subscription: free, but a CEILING reference (its own harness),
    # so cost is 0 and turns/tokens are not comparable to OpenRouter candidates. Anchors
    # are claude-code; candidates are openrouter.
    provider: Literal["openrouter", "claude-code"] = "openrouter"
    retired_reason: str | None = None
    retired_date: str | None = None
    # Short display name for the leaderboard UI. Falls back to the id (minus the
    # provider prefix) when unset, so the plot labels stay controllable from here
    # rather than being string-munged in the frontend.
    display_name: str | None = None
    # Request `model` field, if different from the registry id. Used when the
    # served alias (llama.cpp `--alias`) is not the leaderboard slug.
    api_model: str | None = None
    # Extra JSON keys merged into every /chat/completions payload (for example
    # chat_template_kwargs). Required fields always win over keys here.
    extra_body: dict[str, Any] = Field(default_factory=dict)


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
