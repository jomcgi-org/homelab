"""The static Grimoire UI: private caching, CSP and a strict asset fallback."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grimoire import web

INDEX = (
    '<!doctype html><html><head><script type="module" crossorigin '
    'src="/grimoire/next/assets/index-abc123.js"></script></head>'
    '<body><div id="root"></div></body></html>'
)
STYLE_HASH = "sha256-K63bSR1LetGtaTb/2K6EIsLtf78i2//qEkOoBZCNcBQ="


@pytest.fixture(name="client")
def client_fixture(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(INDEX)
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)")
    (tmp_path / "style-hashes.json").write_text(f'["{STYLE_HASH}"]')
    monkeypatch.setenv("GRIMOIRE_WEB_DIR", str(tmp_path))
    app = FastAPI()
    web.mount_web(app)
    return TestClient(app)


def test_base_path_comes_from_the_bundle():
    assert web.web_base(INDEX) == "/grimoire/next"
    assert web.web_base(INDEX.replace("/grimoire/next/", "/grimoire/")) == "/grimoire"
    with pytest.raises(ValueError):
        web.web_base("<html></html>")


@pytest.mark.parametrize(
    "path",
    [
        "/grimoire/next",
        "/grimoire/next/",
        "/grimoire/next/sheets",
        "/grimoire/next/campaigns/1b2c/session",
    ],
)
def test_app_routes_answer_with_the_private_shell(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["cloudflare-cdn-cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    policy = response.headers["content-security-policy"]
    assert "script-src 'self';" in policy
    assert f"style-src 'self' '{STYLE_HASH}';" in policy
    assert "unsafe" not in policy


def test_hashed_assets_are_cached_by_the_browser_only(client):
    response = client.get("/grimoire/next/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == ("private, max-age=31536000, immutable")
    assert response.headers["cloudflare-cdn-cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "name", ["missing-123.js", "..%2Findex.html", "%2e%2e%2f%2e%2e%2fetc%2fpasswd"]
)
def test_a_missing_asset_is_a_404_not_the_shell(client, name):
    response = client.get(f"/grimoire/next/assets/{name}")
    assert response.status_code == 404
    assert "root" not in response.text
    assert response.headers["cloudflare-cdn-cache-control"] == "no-store"


def test_nothing_is_served_outside_the_base(client):
    assert client.get("/grimoire").status_code == 404
    assert client.get("/api/grimoire/lobby").status_code == 404
    assert client.post("/grimoire/next/").status_code == 405


def test_absent_bundle_registers_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_WEB_DIR", str(tmp_path / "missing"))
    app = FastAPI()
    web.mount_web(app)
    assert TestClient(app).get("/grimoire/next/").status_code == 404


def test_policy_allows_only_well_formed_style_hashes():
    assert "style-src 'self';" in web.content_security_policy([])
    for bad in ["'unsafe-inline'", "sha256-short", "sha256-" + "A" * 43 + "=' *"]:
        with pytest.raises(ValueError):
            web.content_security_policy([bad])
