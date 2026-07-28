"""Tier capability gates and retained legacy guest-env parsing.

A tier is the model endpoint plus the secret PLACEHOLDERS the guest is allowed to
hold, so it is the credential trust boundary. The runner merges the selected
tier's env into the ``AgentRequest.env`` it POSTs to fc-invoke, which injects it
into the guest microVM; the placeholders (``kloak:...``) stay inert until the
fc-invoke egress proxy swaps them for real secrets at the egress hop.

``env_for_tier`` and its ``GOOSECRACKER_TIERS`` input are now unused by the
removed guest path, but remain for compatibility with stored configuration and
their focused tests. Capability gating remains active independently.

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


# --- Per-tier capability (tool) subset (ADR 034, ADR 039 household) ---------
#
# A tier is the credential trust boundary; it also bounds which capability
# families a principal on that tier may use. This is the authoritative
# tier -> allowed-feature mapping the dispatch-time check consults. ADR 034's
# enforced guest-facing MCP endpoint (bearer -> toolset) is not built yet, so
# nothing calls this at guest-MCP dispatch time today: Phase 4 wires the
# session-dispatch check to it. The household tier (ADR 039, amended) gets every
# LOCAL capability (knowledge, calendar, reminders, and artifact/chart builds);
# only repo and cluster stay denied, because those are the two families that
# would need external credentials (a GitHub token, a kubeconfig) in the guest,
# and the household channel deliberately carries none. The boundary is the
# channel, not the toolset: household does everything the trusted tiers do that
# does not require a credential the partner-phone guest must not hold.
_HOUSEHOLD_FEATURES = frozenset({"knowledge", "calendar", "reminders", "artifact"})

# The full capability set. Any tier not named in _TIER_FEATURES (default,
# artifact) is unrestricted: it gets everything. Named here so a household-tier
# denial is a positive check against a known universe rather than an open set.
_FULL_FEATURES = frozenset(
    {"knowledge", "calendar", "reminders", "repo", "cluster", "artifact"}
)

# Only tiers that are narrower than the full set need an entry. Everything else
# falls back to _FULL_FEATURES.
_TIER_FEATURES: dict[str, frozenset[str]] = {
    "household": _HOUSEHOLD_FEATURES,
}


def features_for_tier(tier: str) -> frozenset[str]:
    """The capability families a principal on ``tier`` may use.

    A tier without a restricted entry (default, artifact, unknown) gets the full
    set; the household tier gets the knowledge/calendar/reminders subset.
    """
    return _TIER_FEATURES.get(tier or _DEFAULT_TIER, _FULL_FEATURES)


def tier_allows(tier: str, feature: str) -> bool:
    """Whether ``tier`` is permitted to use the ``feature`` capability family.

    Deny is the household tier refusing repo/cluster/artifact; allow is any of
    its three granted families or any feature on an unrestricted tier.
    """
    return feature in features_for_tier(tier)


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
