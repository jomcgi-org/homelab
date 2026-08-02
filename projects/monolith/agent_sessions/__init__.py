"""Voice-drivable Claude Code agent sessions."""


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
