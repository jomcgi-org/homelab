"""Tier -> guest env map for goose runs (relocated from the fc-agentd chart).

A tier is the model endpoint plus the secret PLACEHOLDERS the guest is allowed to
hold, so it is the credential trust boundary. The runner merges the selected
tier's env into the ``AgentRequest.env`` it POSTs to fc-invoke, which injects it
into the guest microVM; the placeholders (``kloak:...``) stay inert until the
fc-invoke egress proxy swaps them for real secrets at the egress hop.

The map itself is injected as the ``GOOSECRACKER_TIERS`` env var (a JSON object
``{tier: {ENV_KEY: value}}``) from Helm values, never hardcoded here: the values
carry in-cluster service URLs (OPENAI_HOST, the OTLP endpoint) that would break
silently on a release rename if baked into Python (semgrep
``no-hardcoded-k8s-service-url``).

Tiers:
  default -> in-cluster Qwen on vLLM (the proven cold-run path). The only tier;
    artifacts are just an agent run with no repo, so they run here too.
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


# The egress secret-swap CA (ADR 023 6b) public cert, injected into every tier's
# guest env so goose trusts the sidecar's minted leaf when a placeholder is
# swapped on a TLS host (api.github.com, openrouter.ai). Sourced from the
# fc-invoke-egress-ca Secret via the deployment env (never hardcoded); empty when
# the CA is not deployed, which leaves the swap inert (the guest just holds the
# placeholder).
_CA_ENV_VAR = "EGRESS_CA_CERT"


def env_for_tier(tier: str) -> dict[str, str]:
    """The guest env dict for ``tier`` (empty/unknown falls back to default).

    The egress CA cert is merged in from the process env so the guest can trust
    the swap sidecar's leaf; a tier that sets it explicitly still wins.
    """
    tiers = _load_tiers()
    key = tier or _DEFAULT_TIER
    env = tiers.get(key)
    if env is None and key != _DEFAULT_TIER:
        env = tiers.get(_DEFAULT_TIER)
    out = dict(env or {})
    ca_cert = os.environ.get(_CA_ENV_VAR, "").strip()
    if ca_cert and not out.get(_CA_ENV_VAR):
        out[_CA_ENV_VAR] = ca_cert
    return out
