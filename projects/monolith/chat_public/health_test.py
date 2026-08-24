"""Tests for fatal chat public inference health."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

import chat_public.health as health
import chat_public.module
from app.modules_public import PUBLIC_MODULES
from chat_public.module import MODULE
from framework import PUBLIC_PROFILE, build_app


class FakeClient:
    def __init__(
        self,
        *,
        status_code: int | None = None,
        error: httpx.HTTPError | None = None,
    ) -> None:
        self.status_code = status_code
        self.error = error

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def get(self, _url: str) -> httpx.Response:
        if self.error is not None:
            raise self.error
        assert self.status_code is not None
        return httpx.Response(self.status_code)


def _patch_client(monkeypatch, client: FakeClient) -> None:
    monkeypatch.setattr(health, "_client", lambda _timeout: client)


def _patch_database(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr("core.db.get_engine", lambda: engine)


def test_module_registers_fatal_inference_health():
    assert MODULE.register_health == {"inference": health.inference_health}
    assert MODULE.register_health_advisory is None


def test_chat_public_module_is_wired_into_public_app():
    assert chat_public.module.MODULE in PUBLIC_MODULES


@pytest.mark.asyncio
async def test_inference_health_accepts_200(monkeypatch):
    monkeypatch.setenv(health.INFERENCE_URL_ENV, "http://inference.test")
    _patch_client(monkeypatch, FakeClient(status_code=200))

    result = await health.inference_health()

    assert result["ok"] is True
    assert "ms" in result["detail"]


@pytest.mark.asyncio
async def test_inference_health_rejects_503(monkeypatch):
    monkeypatch.setenv(health.INFERENCE_URL_ENV, "http://inference.test")
    _patch_client(monkeypatch, FakeClient(status_code=503))

    result = await health.inference_health()

    assert result["ok"] is False
    assert "503" in result["detail"]


@pytest.mark.asyncio
async def test_inference_health_reports_connect_error(monkeypatch):
    monkeypatch.setenv(health.INFERENCE_URL_ENV, "http://inference.test")
    error = httpx.ConnectError(
        "connection failed", request=httpx.Request("GET", "http://inference.test")
    )
    _patch_client(monkeypatch, FakeClient(error=error))

    result = await health.inference_health()

    assert result["ok"] is False
    assert "unreachable" in result["detail"]


@pytest.mark.asyncio
async def test_inference_health_fails_open_when_unconfigured(monkeypatch):
    monkeypatch.delenv(health.INFERENCE_URL_ENV, raising=False)

    result = await health.inference_health()

    assert result["ok"] is True
    assert health.INFERENCE_URL_ENV in result["detail"]


def test_public_health_fails_for_unreachable_inference(monkeypatch):
    monkeypatch.setenv(health.INFERENCE_URL_ENV, "http://inference.test")
    error = httpx.ConnectError(
        "connection failed", request=httpx.Request("GET", "http://inference.test")
    )
    _patch_client(monkeypatch, FakeClient(error=error))
    _patch_database(monkeypatch)

    response = TestClient(build_app(PUBLIC_PROFILE, [MODULE])).get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["components"]["inference"]["ok"] is False
    assert "inference" not in body.get("degraded", [])


def test_public_health_is_healthy_for_healthy_inference(monkeypatch):
    monkeypatch.setenv(health.INFERENCE_URL_ENV, "http://inference.test")
    _patch_client(monkeypatch, FakeClient(status_code=200))
    _patch_database(monkeypatch)

    response = TestClient(build_app(PUBLIC_PROFILE, [MODULE])).get("/api/health")

    assert response.status_code == 200
    assert response.json()["components"]["inference"]["ok"] is True
