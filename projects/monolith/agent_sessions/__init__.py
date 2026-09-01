"""Voice-drivable Claude Code agent sessions."""

import os

# Every model the user-facing picker may offer, ordered by family. This is the
# single source for selectable models: the Discord /agent choice list and GET
# /api/agents/models are built from it. The runtime still recognizes qwen for
# persisted sessions and explicit compatibility calls, but it is not offered.
#
# Deliberately no FIRST-PARTY imports in this module. chat.bot reads
# SUPPORTED_MODELS at import time to build its slash-command choices, and any
# first-party module imported here would land in that path, dragging a Bazel
# dep onto every target that imports chat.bot without one (see the drift test
# in chat/bot_on_message_test.py). The stdlib os import below is safe: it ships
# with every interpreter.
SUPPORTED_MODELS = ("luna", "terra", "sol", "opus", "sonnet", "fable")

# Per-env allowlist narrowing what the console picker and the Discord /agent
# command OFFER (issue #4859). Comma-separated names; empty or unset means
# every supported model. This is configuration we own, not a boundary: names
# here never widen past SUPPORTED_MODELS, and unknown names are ignored rather
# than rejected. Wired from chart value agents.models.
MODEL_ENV_VAR = "AGENT_MODELS"


def offered_models(configured: str | None = None) -> tuple[str, ...]:
    """Return SUPPORTED_MODELS narrowed by the AGENT_MODELS allowlist.

    ``configured`` overrides the environment read for tests; None means read
    MODEL_ENV_VAR. An empty or unset allowlist means "offer everything": prod
    keeps the full selection by default while dev narrows it to the free
    in-cluster lane with a single values edit.
    """
    raw = os.environ.get(MODEL_ENV_VAR, "") if configured is None else configured
    allowed = {part.strip() for part in raw.split(",") if part.strip()}
    if not allowed:
        return SUPPORTED_MODELS
    return tuple(model for model in SUPPORTED_MODELS if model in allowed)


def model_family(model: str | None) -> str:
    """Return the adapter family for a supported model name."""
    if model == "qwen":
        return "pi"
    if model in {"luna", "terra", "sol"}:
        return "codex"
    if model in {None, "opus", "sonnet", "fable"}:
        return "claude"
    raise ValueError(
        f"Unknown model {model!r}; valid models: opus, sonnet, fable, luna, terra, sol, qwen"
    )


# The EmberVM workload names a session lands on, keyed by family. These are a
# CROSS-SERVICE CONTRACT with the embervm chart's workload CRs
# (projects/embervm/chart/templates/workload-claude-runtime.yaml,
# workload-pi-runtime.yaml), not a local label: the noded egress allowlist
# (EMBERVM_NODED_EGRESS_WORKLOADS) is derived from those same workload names.
# A name that drifts here does not fail loudly. It dial-timeouts inside the
# guest, because the egress lane for an unrecognised workload is simply never
# armed.
#
# The other half of the same contract: the pi workload must stay enabled while
# persisted or explicitly requested qwen sessions remain supported. Production
# no longer offers qwen in its model catalogue, and background work uses Luna.
#
# There is deliberately no workload_for_family() helper here. The live decision
# is transport._workload_for, which consults the env-resolved
# transport.PI_WORKLOAD so the AGENT_PI_WORKLOAD revert lever is honoured. A
# second mapping function reading these raw constants would look like the
# routing mechanism while silently bypassing that lever.
DEFAULT_WORKLOAD = "claude-runtime"
PI_WORKLOAD = "pi-runtime"
