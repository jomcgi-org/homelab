"""The static Grimoire UI: private caching, CSP and a strict asset fallback."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grimoire import web

INDEX = (
    '<!doctype html><html><head><script type="module" crossorigin '
    'src="/grimoire/assets/index-abc123.js"></script></head>'
    '<body><div id="root"></div></body></html>'
)
STYLE_HASH = "sha256-K63bSR1LetGtaTb/2K6EIsLtf78i2//qEkOoBZCNcBQ="


def test_server_audio_scheme_is_allowed_by_shell_policy():
    relative = Path("projects/monolith/web/grimoire/src/lib/fixtures/tts-media.json")
    roots = (
        Path(os.environ.get("TEST_SRCDIR", "")) / "_main",
        Path(__file__).resolve().parents[3],
    )
    fixture = next(root / relative for root in roots if (root / relative).is_file())
    scheme = json.loads(fixture.read_text())["scheme"]
    directives = dict(
        directive.split(" ", 1)
        for directive in web.content_security_policy([]).split("; ")
    )
    assert scheme in directives["media-src"].split()
    assert "data:" not in directives["media-src"].split()


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
    assert web.web_base(INDEX) == "/grimoire"
    assert (
        web.web_base(INDEX.replace("/grimoire/", "/grimoire/next/")) == "/grimoire/next"
    )
    with pytest.raises(ValueError):
        web.web_base("<html></html>")


@pytest.mark.parametrize(
    "path",
    [
        "/grimoire",
        "/grimoire/",
        "/grimoire/sheets",
        "/grimoire/campaigns/1b2c/session",
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
    response = client.get("/grimoire/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == ("private, max-age=31536000, immutable")
    assert response.headers["cloudflare-cdn-cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "name", ["missing-123.js", "..%2Findex.html", "%2e%2e%2f%2e%2e%2fetc%2fpasswd"]
)
def test_a_missing_asset_is_a_404_not_the_shell(client, name):
    response = client.get(f"/grimoire/assets/{name}")
    assert response.status_code == 404
    assert "root" not in response.text
    assert response.headers["cloudflare-cdn-cache-control"] == "no-store"


def test_nothing_is_served_outside_the_base(client):
    assert client.get("/grimoir").status_code == 404
    assert client.get("/moving").status_code == 404
    assert client.get("/api/grimoire/lobby").status_code == 404
    assert client.post("/grimoire/").status_code == 405


def test_absent_bundle_registers_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_WEB_DIR", str(tmp_path / "missing"))
    app = FastAPI()
    web.mount_web(app)
    assert TestClient(app).get("/grimoire/").status_code == 404


def test_policy_allows_only_well_formed_style_hashes():
    assert "style-src 'self';" in web.content_security_policy([])
    for bad in ["'unsafe-inline'", "sha256-short", "sha256-" + "A" * 43 + "=' *"]:
        with pytest.raises(ValueError):
            web.content_security_policy([bad])


# Every page the React router serves (web/grimoire/src/App.tsx), as a browser
# requests it on a reload or from a link.
PAGES = [
    "/grimoire",
    "/grimoire/sheets",
    "/grimoire/join/accept",
    "/grimoire/platform",
    "/grimoire/platform/profile",
    "/grimoire/feedback",
    "/grimoire/roadmap",
    "/grimoire/campaigns/1b2c/session",
    "/grimoire/campaigns/1b2c/notes",
    "/grimoire/campaigns/1b2c/journal",
    "/grimoire/campaigns/1b2c/grants",
    "/grimoire/campaigns/1b2c/entities/9f",
]


@pytest.mark.parametrize("path", PAGES)
def test_a_page_never_shares_its_path_with_a_route_of_another_method(
    path, tmp_path, monkeypatch
):
    """A GET that only partly matches an earlier route breaks request tracing.

    The OpenTelemetry FastAPI instrumentation walks ``app.routes`` and reads
    ``.path`` from a partial match (right path, wrong method). An included
    router has no ``.path``, so a page whose path is also a POST-only endpoint
    answered 500 in production while every test without tracing passed.
    """
    from starlette.routing import Match

    import grimoire

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(INDEX)
    monkeypatch.setenv("GRIMOIRE_WEB_DIR", str(tmp_path))
    app = FastAPI()
    grimoire.register(app)
    scope = {"type": "http", "method": "GET", "path": path, "root_path": ""}
    for route in app.routes:
        match, _ = route.matches(scope)
        assert match != Match.PARTIAL, f"{path} partly matches an earlier route"
        if match == Match.FULL:
            return
    pytest.fail(f"no route serves {path}")
