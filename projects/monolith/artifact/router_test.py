"""Tests for the artifact HTTP surface (ADR 024)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from artifact import s3
from artifact.router import read_router, write_router


@pytest.fixture
def client(monkeypatch) -> TestClient:
    # In-memory artifact store keyed by id, so the handlers exercise their real
    # logic while S3 is faked (the router lazy-imports `artifact.s3`).
    store: dict[str, bytes] = {}

    def fake_put(artifact_id: str, html: bytes) -> str:
        store[artifact_id] = html
        return f"etag-{len(html)}"

    def fake_get(artifact_id: str):
        if artifact_id not in store:
            return None
        return store[artifact_id], f"etag-{len(store[artifact_id])}"

    def fake_head(artifact_id: str):
        if artifact_id not in store:
            return None
        return f"etag-{len(store[artifact_id])}"

    monkeypatch.setattr(s3, "put_artifact", fake_put)
    monkeypatch.setattr(s3, "get_artifact", fake_get)
    monkeypatch.setattr(s3, "head_artifact", fake_head)
    monkeypatch.setenv("ARTIFACT_PUBLIC_BASE", "https://jomcgi.dev")

    app = FastAPI()
    app.include_router(write_router)
    app.include_router(read_router)
    return TestClient(app)


def test_publish_assigns_id_and_returns_url(client: TestClient):
    resp = client.post("/internal/artifact", json={"html": "<h1>hi</h1>"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("id")
    assert body.get("url") == f"https://jomcgi.dev/artifact/{body.get('id')}"
    assert body.get("version")


def test_publish_honours_supplied_id(client: TestClient):
    resp = client.post(
        "/internal/artifact", json={"id": "my-demo_1", "html": "<p>x</p>"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("id") == "my-demo_1"


def test_publish_rejects_invalid_id(client: TestClient):
    resp = client.post(
        "/internal/artifact", json={"id": "../etc/passwd", "html": "<p>x</p>"}
    )
    assert resp.status_code == 422


def test_publish_rejects_empty_html(client: TestClient):
    resp = client.post("/internal/artifact", json={"html": ""})
    assert resp.status_code == 422


def test_publish_rejects_oversized_html(client: TestClient):
    big = "x" * (2 * 1024 * 1024 + 1)
    resp = client.post("/internal/artifact", json={"html": big})
    assert resp.status_code == 413


def test_raw_serves_html_with_sandboxed_csp(client: TestClient):
    client.post("/internal/artifact", json={"id": "demo", "html": "<h1>hello</h1>"})
    resp = client.get("/internal/artifact/demo/raw")
    assert resp.status_code == 200
    assert resp.text == "<h1>hello</h1>"
    assert resp.headers["content-type"].startswith("text/html")
    csp = resp.headers["content-security-policy"]
    # The artifact tier security invariant (ADR 024 decision 4 + 2026-06-29
    # amendment): the opaque-origin sandbox is the boundary that protects our
    # origin and must never regress. The CSP is intentionally open to the https
    # web (CDN libs, fonts, live API fetch) so artifacts behave like normal pages.
    assert "sandbox allow-scripts" in csp
    assert "allow-same-origin" not in csp  # never re-grant the real origin
    assert "default-src 'none'" in csp
    assert "connect-src https:" in csp  # live data / API refresh allowed
    assert "http:" not in csp  # https only: no plaintext / LAN-probe downgrade
    assert resp.headers["cache-control"] == "no-store"


def test_raw_missing_is_404(client: TestClient):
    assert client.get("/internal/artifact/nope/raw").status_code == 404


def test_raw_invalid_id_is_404(client: TestClient):
    assert client.get("/internal/artifact/..%2f..%2fx/raw").status_code == 404


def test_version_returns_etag(client: TestClient):
    client.post("/internal/artifact", json={"id": "demo", "html": "<h1>hello</h1>"})
    resp = client.get("/internal/artifact/demo/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": "etag-14"}
    assert resp.headers["cache-control"] == "no-store"


def test_version_missing_is_404(client: TestClient):
    assert client.get("/internal/artifact/nope/version").status_code == 404
