"""Verify that FastAPI serves SvelteKit static frontend files."""

import os
import tempfile
from pathlib import Path

import pytest

# Create a temp dir with minimal frontend output BEFORE importing the app,
# since the StaticFiles mount runs at module-import time.
_static_dir = tempfile.mkdtemp()
Path(_static_dir, "index.html").write_text(
    "<!doctype html><html><body>Monolith Frontend</body></html>"
)
Path(_static_dir, "favicon.png").write_bytes(b"\x89PNG fake")
os.environ["STATIC_DIR"] = _static_dir

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(name="client")
def client_fixture():
    return TestClient(app, raise_server_exceptions=False)


def test_serves_index_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Monolith Frontend" in response.text


def test_serves_static_asset(client):
    response = client.get("/favicon.png")
    assert response.status_code == 200
    assert b"PNG" in response.content


def test_api_routes_not_shadowed(client):
    """API routes registered before the catch-all static mount still work."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "chart_version": None}
