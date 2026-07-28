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


def test_credentials_arrive_only_by_secret_ref() -> None:
    """No entry may carry an inline value; that is how one gets committed."""
    for entry in _egress_secrets():
        assert entry.get("secretRef"), (
            f"egress secret {entry.get('env')!r} has no secretRef. Credentials "
            "must come from a Secret, never from a literal in values.yaml."
        )
        assert "value" not in entry, (
            f"egress secret {entry.get('env')!r} carries an inline value."
        )
