"""Unit tests for cluster/router.py — the /api/cluster verdict route."""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

import cluster.module
import cluster.router
from framework import PRIVATE_PROFILE, build_app

# Compose only the cluster domain: the production framework wiring without
# importing every other domain.
app = build_app(
    dataclasses.replace(PRIVATE_PROFILE, otel_enabled=False),
    (cluster.module.MODULE,),
)


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_verdict_returns_the_tool_result_unchanged(client, monkeypatch):
    seen = {}

    async def _verify(name, expected_revision=None):
        seen["args"] = (name, expected_revision)
        return {"verdict": "verified", "app": name}

    monkeypatch.setattr(cluster.router, "verify_deployment", _verify)

    resp = client.get(
        "/api/cluster/applications/monolith/verdict",
        params={"expected_revision": "0.559.0"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"verdict": "verified", "app": "monolith"}
    assert seen["args"] == ("monolith", "0.559.0")


def test_verdict_omits_expected_revision_by_default(client, monkeypatch):
    seen = {}

    async def _verify(name, expected_revision=None):
        seen["args"] = (name, expected_revision)
        return {"error": f"application {name!r} not found in argocd"}

    monkeypatch.setattr(cluster.router, "verify_deployment", _verify)

    resp = client.get("/api/cluster/applications/nope/verdict")

    assert resp.status_code == 200
    assert resp.json() == {"error": "application 'nope' not found in argocd"}
    assert seen["args"] == ("nope", None)
