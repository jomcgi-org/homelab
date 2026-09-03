"""Shared configuration and HTTP authentication for chat inference.

Meta Spark uses the OpenAI-compatible API shape. Its bearer token is optional
in local and test environments, so callers use ``auth_headers`` rather than
constructing an Authorization header themselves. ``structured_output`` remains
the compatibility seam for the separate Grimoire extraction providers.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

META_SPARK_MODEL = "muse-spark-1.3-contributor"
META_SPARK_API_KEY_ENV = "META_SPARK_API_KEY"

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
