import json
from types import SimpleNamespace

import pytest

from tools.session_collector.base_url import (
    DEFAULT_BASE_URL,
    resolve_auth_mode,
    resolve_base_url,
)


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://monolith.example.ts.net", "none"),
        ("https://private.jomcgi.dev", "cloudflare"),
    ],
)
def test_auto_auth_uses_tailnet_hostname(base_url, expected):
    assert resolve_auth_mode("auto", base_url) == expected


def test_tailscale_status_supplies_magic_dns_suffix(monkeypatch):
    calls = []

    def status(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"MagicDNSSuffix": "example.ts.net"}),
        )

    monkeypatch.setattr(
        "tools.session_collector.base_url.shutil.which", lambda name: "/bin/tailscale"
    )
    monkeypatch.setattr("tools.session_collector.base_url.subprocess.run", status)

    assert resolve_base_url({}) == "http://monolith.example.ts.net"
    assert calls[0][0] == ["/bin/tailscale", "status", "--json"]


def test_environment_override_wins_without_tailscale_probe(monkeypatch):
    monkeypatch.setattr(
        "tools.session_collector.base_url.subprocess.run",
        lambda *args, **kwargs: pytest.fail("tailscale must not be called"),
    )
    assert (
        resolve_base_url({"SESSION_COLLECTOR_BASE_URL": "http://override.test"})
        == "http://override.test"
    )


def test_missing_tailscale_falls_back_to_cloudflare(monkeypatch):
    monkeypatch.setattr(
        "tools.session_collector.base_url.shutil.which", lambda name: None
    )
    monkeypatch.setattr(
        "tools.session_collector.base_url.TAILSCALE_APP_BINARY",
        type("MissingPath", (), {"is_file": lambda self: False})(),
    )
    assert resolve_base_url({}) == DEFAULT_BASE_URL
