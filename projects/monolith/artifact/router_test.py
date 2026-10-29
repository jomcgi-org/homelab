"""Tests for the artifact HTTP surface (ADR 024)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from artifact import register, register_public, s3


@pytest.fixture(params=[register, register_public], ids=["full", "public"])
def client(monkeypatch, request) -> TestClient:
    # In-memory artifact store keyed by id, so the handlers exercise their real
    # logic while S3 is faked (the router lazy-imports `artifact.s3`).
    store = {
        "demo": b"<h1>hello</h1>",
        "my-demo_1": b"<p>x</p>",
        "a" * 64: b"<p>max id</p>",
        "a": b"<p>min id</p>",
    }

    def fake_get(artifact_id: str):
        if artifact_id not in store:
            return None
        return store[artifact_id], f"etag-{len(store[artifact_id])}"

    def fake_head(artifact_id: str):
        if artifact_id not in store:
            return None
        return f"etag-{len(store[artifact_id])}"

    monkeypatch.setattr(s3, "get_artifact", fake_get)
    monkeypatch.setattr(s3, "head_artifact", fake_head)

    app = FastAPI()
    request.param(app)
    return TestClient(app)


def test_publish_is_not_mounted(client: TestClient):
    resp = client.post("/internal/artifact", json={"html": "<h1>hi</h1>"})
    assert resp.status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) == {
        "/internal/artifact/{artifact_id}/raw",
        "/internal/artifact/{artifact_id}/version",
    }
    assert all(set(operations) == {"get"} for operations in paths.values())


def test_raw_serves_html_with_sandboxed_csp(client: TestClient):
    resp = client.get("/internal/artifact/demo/raw")
    assert resp.status_code == 200
    assert resp.text == "<h1>hello</h1>"
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
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
    assert csp == (
        "sandbox allow-scripts; default-src 'none'; "
        "script-src 'unsafe-inline' https:; style-src 'unsafe-inline' https:; "
        "img-src data: blob: https:; font-src data: https:; "
        "connect-src https:; form-action 'none'; base-uri 'none'"
    )
    assert "script-src 'unsafe-inline' https:" in csp
    assert "style-src 'unsafe-inline' https:" in csp
    assert "img-src data: blob: https:" in csp
    assert "font-src data: https:" in csp
    assert "form-action 'none'" in csp
    assert "base-uri 'none'" in csp
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_raw_missing_is_404(client: TestClient):
    assert client.get("/internal/artifact/nope/raw").status_code == 404


@pytest.mark.parametrize("endpoint", ["raw", "version"])
@pytest.mark.parametrize("artifact_id", ["%2e", "bad.id", "a" * 65])
def test_invalid_id_is_404_without_storage(client, monkeypatch, endpoint, artifact_id):
    def unexpected_read(_artifact_id):
        pytest.fail("invalid artifact id reached storage")

    monkeypatch.setattr(s3, "get_artifact", unexpected_read)
    monkeypatch.setattr(s3, "head_artifact", unexpected_read)
    response = client.get(f"/internal/artifact/{artifact_id}/{endpoint}")
    assert response.status_code == 404
    # Distinguish the handler's rejection from a router miss or URL normalization.
    assert response.json() == {"detail": "not found"}


@pytest.mark.parametrize("endpoint", ["raw", "version"])
def test_encoded_path_separators_do_not_match_read_routes(client, endpoint):
    response = client.get(f"/internal/artifact/..%2f..%2fx/{endpoint}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("endpoint", ["raw", "version"])
@pytest.mark.parametrize("artifact_id", ["my-demo_1", "a", "a" * 64])
def test_valid_id_reads_stored_artifact(client, endpoint, artifact_id):
    assert client.get(f"/internal/artifact/{artifact_id}/{endpoint}").status_code == 200


def test_version_returns_etag(client: TestClient):
    resp = client.get("/internal/artifact/demo/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": "etag-14"}
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers["cache-control"] == "no-store"


def test_version_missing_is_404(client: TestClient):
    assert client.get("/internal/artifact/nope/version").status_code == 404
