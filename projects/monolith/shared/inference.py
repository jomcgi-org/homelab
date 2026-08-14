"""Vendor-neutral request fragments for the in-cluster chat inference dialect.

This module is the single place for compatibility between the in-cluster vLLM
and llama.cpp engines. The helpers return fragments that callers can merge into
either PydanticAI ``extra_body`` mappings or raw HTTP JSON bodies, so transport
style remains local to each caller.

``thinking_off`` preserves the option used by callers that need their token
budget for visible content. ``reasoning_effort`` validates the values accepted
by Qwen's chat template, which explicitly raises for values outside ``xhigh``,
``medium``, and ``low``. ``structured_output`` sends both vendor extensions:
vLLM honors ``guided_json`` while llama.cpp honors the equivalent JSON Schema
``response_format``. They encode the same schema, so whichever field the live
engine supports supplies the constraint and the other is inert, without adding
new engine-specific configuration coupling.
"""

from __future__ import annotations

from typing import Any

REASONING_EFFORTS: frozenset[str] = frozenset({"xhigh", "medium", "low"})


def thinking_off() -> dict[str, dict[str, bool]]:
    """Return the chat-template options that disable Qwen thinking."""
    return {"chat_template_kwargs": {"enable_thinking": False}}


def reasoning_effort(effort: str) -> dict[str, dict[str, str]]:
    """Return a validated Qwen chat-template reasoning-effort fragment."""
    if effort not in REASONING_EFFORTS:
        legal = ", ".join(sorted(REASONING_EFFORTS))
        raise ValueError(f"reasoning effort must be one of: {legal}; got {effort!r}")
    return {"chat_template_kwargs": {"reasoning_effort": effort}}


def structured_output(schema: dict[str, Any], *, name: str) -> dict[str, Any]:
    """Return vLLM and llama.cpp structured-output extensions for ``schema``.

    vLLM honors ``guided_json`` and llama.cpp honors the equivalent JSON Schema
    ``response_format``. Both reference the same schema, so the active engine
    applies the same constraint while the unsupported vendor field is inert.
    """
    return {
        "guided_json": schema,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": name,
                "strict": True,
                "schema": schema,
            },
        },
    }
