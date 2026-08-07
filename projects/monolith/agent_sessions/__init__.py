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
