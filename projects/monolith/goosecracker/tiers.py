"""Tier -> guest env map for goose runs (relocated from the fc-agentd chart).

A tier is the model endpoint plus the secret PLACEHOLDERS the guest is allowed to
hold, so it is the credential trust boundary. The runner merges the selected
tier's env into the ``AgentRequest.env`` it POSTs to fc-invoke, which injects it
into the guest microVM; the placeholders (``kloak:...``) stay inert until the
fc-invoke egress proxy swaps them for real secrets at the egress hop.

The map itself is injected as the ``GOOSECRACKER_TIERS`` env var (a JSON object
``{tier: {ENV_KEY: value}}``) from Helm values, never hardcoded here: the values
carry in-cluster service URLs (OPENAI_HOST, the OTLP endpoint,
ARTIFACT_PUBLISH_URL) that would break silently on a release rename if baked into
Python (semgrep ``no-hardcoded-k8s-service-url``).

Tiers:
  default -> in-cluster Qwen on vLLM (the proven cold-run path).
  artifact -> Gemini via OpenRouter. Kept, but the OpenRouter key is a placeholder
    the fc-invoke egress proxy must swap on openrouter.ai; until that egress
    secret-swap lands, the artifact tier will not reach the model. Correctness
    here is focused on the default/Qwen tier.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_ENV_VAR = "GOOSECRACKER_TIERS"

# An empty/legacy tier falls back to this key (in-cluster Qwen).
_DEFAULT_TIER = "default"


def _load_tiers() -> dict[str, dict[str, str]]:
    """Parse the GOOSECRACKER_TIERS JSON env into a {tier: {key: value}} map.

    Returns an empty map when unset or unparseable, so a misconfiguration
    surfaces as an empty guest env (a clear run failure) rather than a crash.
    """
    raw = os.environ.get(_ENV_VAR, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.exception("goosecracker: GOOSECRACKER_TIERS is not valid JSON")
        return {}
    if not isinstance(data, dict):
        logger.error("goosecracker: GOOSECRACKER_TIERS must be a JSON object")
        return {}
    out: dict[str, dict[str, str]] = {}
    for tier, env in data.items():
        if isinstance(env, dict):
            out[str(tier)] = {str(k): str(v) for k, v in env.items()}
    return out


def env_for_tier(tier: str) -> dict[str, str]:
    """The guest env dict for ``tier`` (empty/unknown falls back to default)."""
    tiers = _load_tiers()
    key = tier or _DEFAULT_TIER
    env = tiers.get(key)
    if env is None and key != _DEFAULT_TIER:
        env = tiers.get(_DEFAULT_TIER)
    return dict(env or {})
