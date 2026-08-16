"""Voice-drivable Claude Code agent sessions."""

# Every model a caller may name, ordered by family (codex, claude, pi). This is
# the single source for selectable models: the Discord /agent choice list is
# built from it, so a model added here appears there without a second edit and
# the two can never disagree about what model_family accepts.
#
# Deliberately no imports in this module. chat.bot reads SUPPORTED_MODELS at
# import time to build its slash-command choices, and anything imported here
# would land in that path (see the agent_sessions.api cycle in chat/bot.py).
SUPPORTED_MODELS = ("luna", "terra", "sol", "opus", "sonnet", "fable", "qwen")


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
DEFAULT_WORKLOAD = "claude-runtime"
PI_WORKLOAD = "pi-runtime"


def workload_for_family(family: str) -> str:
    """Return the EmberVM workload name a session of this family creates on."""
    if family == "pi":
        return PI_WORKLOAD
    return DEFAULT_WORKLOAD
