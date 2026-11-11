"""Authenticated HTTP access to the private homelab API."""

from __future__ import annotations

import httpx
import typer

from tools.cli.auth import DEFAULT_HOSTNAME, clear_cf_token, get_cf_token

API_BASE = f"https://{DEFAULT_HOSTNAME}"


def client() -> httpx.Client:
    token = get_cf_token()
    return httpx.Client(
        base_url=API_BASE,
        cookies={"CF_Authorization": token},
        follow_redirects=False,
        timeout=30.0,
    )


def request(method: str, path: str, **kwargs) -> httpx.Response:
    """Make a request with automatic re-auth on 3xx (expired CF token)."""
    with client() as http:
        resp = getattr(http, method)(path, **kwargs)

    if resp.is_redirect:
        typer.echo("Token expired, re-authenticating...", err=True)
        clear_cf_token()
        with client() as http:
            resp = getattr(http, method)(path, **kwargs)

    return resp
