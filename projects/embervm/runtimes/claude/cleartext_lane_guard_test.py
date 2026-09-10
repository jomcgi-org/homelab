"""Guard (ADR 023 6b): the cleartext egress lane must always be credentialed.

The claude guest points its API client at an `http://` URL on purpose: its only
route out is a host-local vsock to the egress-proxy sidecar, which injects the
real credential and originates verified TLS to :443 itself.

That arrangement has one sharp edge. If the sidecar has no catalog entry for the
host the guest addresses in cleartext, the connection falls through to the blind
tunnel and the full request, prompt and all, leaves the cluster UNENCRYPTED over
the public internet. The sidecar now fails closed at runtime, but nothing stops
someone deleting the catalog entry while leaving the guest pointed at http://,
which is the configuration that produces the leak.

So this asserts the pairing: if guest-init sets ANTHROPIC_BASE_URL to an http://
host, deploy/values.yaml must carry an egress.secrets entry covering that host.

Note what this deliberately does NOT check: byte-equality of any shared string.
The guest holds a login-gate dummy whose value the sidecar discards, so there is
nothing to keep in sync. An earlier design required a byte-identical placeholder
in both files; this guard replaced the test that policed it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

import yaml

BASE_URL_PATTERN = re.compile(r'"ANTHROPIC_BASE_URL"\s*:\s*"([^"]+)"')


def _repo_path(*parts: str) -> Path:
    """Resolve a repo-relative path, in-bazel (TEST_SRCDIR) or standalone."""
    rel = Path(*parts)
    candidate = Path(os.environ.get("TEST_SRCDIR", "")) / "_main" / rel
    if candidate.exists():
        return candidate
    # Direct run: this file lives at projects/embervm/runtimes/claude/.
    here = Path(__file__).resolve().parents[4] / rel
    if here.exists():
        return here
    raise FileNotFoundError(f"{rel} not found at {candidate} or {here}")


def _guest_base_url() -> str | None:
    source = _repo_path(
        "projects/embervm/runtimes/claude/guest-init/cmd/main.go"
    ).read_text()
    match = BASE_URL_PATTERN.search(source)
    return match.group(1) if match else None


def _egress_secrets() -> list[dict]:
    values = yaml.safe_load(
        _repo_path("projects/embervm/deploy/values.yaml").read_text()
    )
    return (values.get("egress") or {}).get("secrets") or []


def _registered_broker_grants() -> set[str]:
    """Grant names the token broker holds on the hub.

    Helm replaces lists, so the hub's values-gke.yaml grants list is the
    whole registry when present; the base values.yaml list applies otherwise.
    """
    for name in ("values-gke.yaml", "values.yaml"):
        values = yaml.safe_load(_repo_path("projects/embervm/deploy", name).read_text())
        grants = (values.get("tokenBroker") or {}).get("grants")
        if grants:
            return {g["name"] for g in grants}
    return set()


def test_cleartext_base_url_has_a_credential_entry() -> None:
    base_url = _guest_base_url()
    assert base_url, (
        "guest-init sets no ANTHROPIC_BASE_URL; if it moved, follow it here"
    )
    if urlparse(base_url).scheme != "http":
        return  # https needs no injection to stay off the wire

    host = urlparse(base_url).hostname
    covered = [s for s in _egress_secrets() if host in (s.get("egressTo") or [])]
    assert covered, (
        f"guest addresses {host} in CLEARTEXT ({base_url}) but no egress.secrets "
        "entry covers it. Without one the sidecar blind-tunnels, and the whole "
        "request, prompt included, leaves the cluster unencrypted."
    )
    assert covered[0].get("header"), (
        f"the egress entry for {host} sets no header, so nothing authenticates "
        "the request the guest sends in cleartext."
    )


def test_credentials_arrive_only_by_managed_source() -> None:
    """No entry may carry an inline value; that is how one gets committed.

    Exactly one managed source per entry: a secretRef (a Kubernetes Secret,
     1Password-synced), a brokerGrant (a short-lived access token fetched
    from the token broker, ADR 048), or a brokerGrants pool of such grants
    (#5974). Each names a credential the sidecar resolves at runtime; none
    puts one in git. An entry naming more than one is ambiguous and an entry
    naming none can never inject, so both are rejected here as well as in
    the chart.
    """
    for entry in _egress_secrets():
        sources = [
            k for k in ("secretRef", "brokerGrant", "brokerGrants") if entry.get(k)
        ]
        assert len(sources) == 1, (
            f"egress entry "
            f"{entry.get('env') or entry.get('brokerGrant') or entry.get('brokerGrants')!r} names "
            f"{sources or 'no'} credential source. Exactly one of secretRef, "
            "brokerGrant or brokerGrants is required; a credential must never be "
            "a literal in values.yaml."
        )
        assert "value" not in entry, (
            f"egress secret {entry.get('env')!r} carries an inline value."
        )


def test_pool_members_are_registered_grants() -> None:
    """Every brokerGrant or brokerGrants member must be a broker grant.

    An unregistered pool member is deliberately non-fatal in the sidecar (it
    is marked dead and retried on a cooldown), so a typo here runs the lane on
    one account forever with nothing red. The registry and the pool live in
    different files, so this is the one place that ties them together.
    """
    registered = _registered_broker_grants()
    assert registered, "no tokenBroker.grants found in deploy values"
    for entry in _egress_secrets():
        members = list(entry.get("brokerGrants") or [])
        if entry.get("brokerGrant"):
            members.append(entry["brokerGrant"])
        for member in members:
            assert member in registered, (
                f"egress entry for {entry.get('egressTo')} names broker grant "
                f"{member!r}, which tokenBroker.grants does not register; the "
                "sidecar would skip it on a cooldown forever."
            )
