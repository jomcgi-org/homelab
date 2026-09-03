"""Resolve the session collector endpoint and authentication mode."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_BASE_URL = "https://private.jomcgi.dev"
TAILSCALE_APP_BINARY = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")


def _tailscale_binary() -> str | None:
    binary = shutil.which("tailscale")
    if binary:
        return binary
    if TAILSCALE_APP_BINARY.is_file() and os.access(TAILSCALE_APP_BINARY, os.X_OK):
        return str(TAILSCALE_APP_BINARY)
    return None


def _tailnet_base_url() -> str | None:
    binary = _tailscale_binary()
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return None
        status = json.loads(result.stdout)
        if status.get("BackendState") != "Running":
            return None
        suffix = status.get("MagicDNSSuffix")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(suffix, str) or not suffix.strip("."):
        return None
    return f"http://monolith.{suffix.strip('.')}"


def resolve_base_url(environ: Mapping[str, str] | None = None) -> str:
    """Use an environment override, the local tailnet, or Cloudflare."""
    environment = os.environ if environ is None else environ
    if "SESSION_COLLECTOR_BASE_URL" in environment:
        return environment["SESSION_COLLECTOR_BASE_URL"]
    return _tailnet_base_url() or DEFAULT_BASE_URL


def resolve_auth_mode(auth: str, base_url: str) -> str:
    """Resolve automatic authentication from the endpoint hostname."""
    if auth != "auto":
        return auth
    hostname = (urlparse(base_url).hostname or "").lower().rstrip(".")
    return "none" if hostname.endswith(".ts.net") else "cloudflare"


if __name__ == "__main__":
    print(resolve_base_url())
