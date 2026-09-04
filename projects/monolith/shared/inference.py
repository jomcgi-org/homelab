"""Shared configuration and HTTP authentication for chat inference.

Meta Spark uses the OpenAI-compatible API shape. Its bearer token is optional
in local and test environments, so callers use ``auth_headers`` rather than
constructing an Authorization header themselves. ``structured_output`` remains
the compatibility seam for the separate Grimoire extraction providers.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from opentelemetry import trace
from opentelemetry.trace import SpanKind

META_SPARK_MODEL = "muse-spark-1.3-contributor"
META_SPARK_API_KEY_ENV = "META_SPARK_API_KEY"
CHAT_REASONING_EFFORT_ENV = "CHAT_REASONING_EFFORT"
_DEFAULT_CHAT_REASONING_EFFORT = "minimal"

logger = logging.getLogger(__name__)

# Decode-slot policy for the shared in-cluster engine.
#
# Synchronous callers (a human is waiting: Discord chat, private chat) are NOT
# capped. They take whatever slots they need, and latency for a waiting human is
# the thing the GPU exists to protect.
#
# Asynchronous callers (grimoire extraction, generation, background jobs) get
# exactly ONE slot each. Bulk work is throughput-shaped, not latency-shaped, so
# the difference between finishing in an hour and ninety minutes does not matter,
# whereas a background job that occupies the whole GPU makes every interactive
# reply queue behind it. This is why the number is 1 rather than "one less than
# the slot count": the goal is that async work is never the reason a human waits.
#
# Import this rather than hardcoding a literal, so the policy has one home.
#
# Caveat worth knowing: this bounds fan-out WITHIN one job process. It is not a
# cross-pod semaphore, so two async jobs running at once can still take two
# slots. chat_public solves the cross-pod version with Postgres advisory locks
# if this ever needs to be enforced globally rather than by convention.
ASYNC_SLOT_BUDGET = 1


def chat_reasoning_effort() -> str:
    """Reasoning depth for the synchronous Discord paths.

    Spark always reasons and none is a 400 from Meta; reasoning tokens bill
    as output. The Discord paths are synchronous with humans waiting, so they
    run at minimal effort. Async callers (swarm, jobs, orchestrator fallbacks)
    keep the provider default. This env var lets the level be tuned and
    reverted without a code deploy.
    """
    return (
        os.environ.get(CHAT_REASONING_EFFORT_ENV, "").strip()
        or _DEFAULT_CHAT_REASONING_EFFORT
    )


CHAT_MAX_TOKENS_ENV = "CHAT_MAX_TOKENS"
_DEFAULT_CHAT_MAX_TOKENS = 512


def chat_max_tokens() -> int | None:
    """Output ceiling for the synchronous Discord paths.

    Discord truncates at 2000 chars (roughly 512 tokens). Tokens past that
    ceiling are billed by the provider and then discarded unread by the client,
    pure waste. This env var gates the cap without reverting commit cf57ba20b:
    that removed a hardcoded max_tokens=16384 against a small context window
    because prompt + max_tokens overflowed. Spark has a 1M token context, so
    512 is safe. A value of 0 or negative returns None, disabling the cap
    entirely (the pre-cf57ba20b baseline); this is values-only so a small-context
    local model can clear it without a code change.
    """
    val = os.environ.get(CHAT_MAX_TOKENS_ENV, "").strip()
    if not val:
        return _DEFAULT_CHAT_MAX_TOKENS
    try:
        num = int(val)
        return None if num <= 0 else num
    except ValueError:
        return _DEFAULT_CHAT_MAX_TOKENS


def record_usage(usage_dict: Mapping[str, Any] | None, model: str, caller: str) -> None:
    """Emit a usage span and decorate an ambient recording span when present."""
    try:
        if not isinstance(usage_dict, Mapping):
            return
        token_fields = ("prompt_tokens", "completion_tokens", "total_tokens")
        if any(field not in usage_dict for field in token_fields):
            return

        attributes = {
            "llm.usage.prompt_tokens": int(usage_dict["prompt_tokens"]),
            "llm.usage.completion_tokens": int(usage_dict["completion_tokens"]),
            "llm.usage.total_tokens": int(usage_dict["total_tokens"]),
            "llm.model": model,
            "llm.caller": caller,
        }
        completion_details = usage_dict.get("completion_tokens_details")
        if isinstance(completion_details, Mapping) and (
            "reasoning_tokens" in completion_details
        ):
            attributes["llm.usage.reasoning_tokens"] = int(
                completion_details["reasoning_tokens"]
            )
        prompt_details = usage_dict.get("prompt_tokens_details")
        if isinstance(prompt_details, Mapping) and ("cached_tokens" in prompt_details):
            attributes["llm.usage.cached_tokens"] = int(prompt_details["cached_tokens"])
        ambient_span = trace.get_current_span()
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(
            "llm.completion", kind=SpanKind.CLIENT
        ) as usage_span:
            usage_span.set_attributes(attributes)

        if ambient_span is not None and ambient_span.is_recording():
            ambient_span.set_attributes(attributes)
    except Exception:
        logger.debug("Failed to record inference token usage", exc_info=True)


def auth_headers(base_url: str | None = None) -> dict[str, str]:
    """Return Meta Spark bearer auth for its host when the key is non-empty."""
    if base_url and urlparse(base_url).hostname != "api.meta.ai":
        return {}
    api_key = os.environ.get(META_SPARK_API_KEY_ENV, "")
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


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
