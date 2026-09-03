"""Cloudflare Access token management.

Reads cached tokens from ~/.cloudflared/ or triggers interactive login
via `cloudflared access login`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

CF_TOKEN_DIR = Path.home() / ".cloudflared"
DEFAULT_HOSTNAME = "private.jomcgi.dev"


def read_cached_cf_token(hostname: str = DEFAULT_HOSTNAME) -> str | None:
    """Return the cached token for *hostname* without starting a login."""
    return _read_token(hostname)


def get_cf_token(hostname: str = DEFAULT_HOSTNAME) -> str:
    """Return a valid Cloudflare Access token for *hostname*.

    Checks ~/.cloudflared/ for an existing token file. If none found,
    runs ``cloudflared access login`` interactively, then reads the
    newly created token.

    Raises SystemExit if cloudflared is not installed.
    """
    token = _read_token(hostname)
    if token:
        return token

    if not shutil.which("cloudflared"):
        raise SystemExit(
            "cloudflared is not installed. "
            "Install it to authenticate with Cloudflare Access."
        )

    subprocess.run(
        ["cloudflared", "access", "login", f"https://{hostname}"],
        check=True,
        timeout=300,  # 5 minutes to complete browser-based auth
    )

    token = _read_token(hostname)
    if not token:
        raise SystemExit(f"Failed to obtain token after login for {hostname}")
    return token


def clear_cf_token(hostname: str = DEFAULT_HOSTNAME) -> None:
    """Remove cached token files for *hostname* so the next call re-auths."""
    if not CF_TOKEN_DIR.is_dir():
        return
    for path in CF_TOKEN_DIR.glob(f"*{hostname}*"):
        path.unlink(missing_ok=True)


def _read_token(hostname: str) -> str | None:
    """Read the most recent token file matching *hostname*."""
    if not CF_TOKEN_DIR.is_dir():
        return None
    matches = sorted(
        CF_TOKEN_DIR.glob(f"*{hostname}*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        return None
    return matches[0].read_text().strip()
