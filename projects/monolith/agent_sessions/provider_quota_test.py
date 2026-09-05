from __future__ import annotations

import dataclasses

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from agent_sessions import provider_quota as quota
from agent_sessions.module import MODULE
from framework import PRIVATE_PROFILE, Module, build_app


class FakeAsyncClient:
    def __init__(self, handler, calls: list[str]) -> None:
        self.handler = handler
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str) -> httpx.Response:
        self.calls.append(url)
        result = self.handler(url)
        if isinstance(result, Exception):
            raise result
        return result


class FakeSyncClient:
    def __init__(self, handler, calls: list[str]) -> None:
        self.handler = handler
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, url: str) -> httpx.Response:
        self.calls.append(url)
        result = self.handler(url)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def _clear_cache():
    quota.reset_cache()
    yield
    quota.reset_cache()


def _response(status_code: int = 200, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload if payload is not None else {"providers": {}},
        request=httpx.Request("GET", "http://broker/quota"),
    )


def _patch_client(monkeypatch, handler, fetch_kind: str):
    calls: list[str] = []
    timeouts = []

    def client(*, timeout):
        timeouts.append(timeout)
        if fetch_kind == "async":
            return FakeAsyncClient(handler, calls)
        return FakeSyncClient(handler, calls)

    client_name = "AsyncClient" if fetch_kind == "async" else "Client"
    monkeypatch.setattr(quota.httpx, client_name, client)
    return calls, timeouts


async def _fetch(fetch_kind: str, *, force: bool = False) -> dict:
    if fetch_kind == "async":
        return await quota.fetch_provider_quota(force=force)
    return quota.fetch_provider_quota_sync(force=force)


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch_kind", ["async", "sync"])
async def test_cache_ttl_force_and_expiry(monkeypatch, fetch_kind):
    monkeypatch.setenv(quota.BROKER_URL_ENV, "http://broker/")
    now = [100.0]
    monkeypatch.setattr(quota.time, "monotonic", lambda: now[0])
    calls, timeouts = _patch_client(
        monkeypatch,
        lambda _url: _response(payload={"providers": {"codex": {}}}),
        fetch_kind,
    )

    first = await _fetch(fetch_kind)
    second = await _fetch(fetch_kind)
    forced = await _fetch(fetch_kind, force=True)
    now[0] += quota.PROVIDER_QUOTA_TTL_SECONDS
    expired = await _fetch(fetch_kind)

    assert first is second
    assert forced["available"] is True
    assert expired["available"] is True
    assert calls == ["http://broker/quota"] * 3
    assert timeouts == [5, 5, 5]


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch_kind", ["async", "sync"])
@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (_response(404), "broker returned 404"),
        (
            httpx.ReadTimeout(
                "slow", request=httpx.Request("GET", "http://broker/quota")
            ),
            "broker request timed out",
        ),
        (
            httpx.ConnectError(
                "refused", request=httpx.Request("GET", "http://broker/quota")
            ),
            "broker unavailable",
        ),
    ],
)
async def test_fetch_failures_are_unavailable(monkeypatch, fetch_kind, failure, reason):
    monkeypatch.setenv(quota.BROKER_URL_ENV, "http://broker")
    _patch_client(monkeypatch, lambda _url: failure, fetch_kind)

    result = await _fetch(fetch_kind)

    assert result == {"available": False, "reason": reason, "providers": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch_kind", ["async", "sync"])
async def test_failed_result_is_cached(monkeypatch, fetch_kind):
    monkeypatch.setenv(quota.BROKER_URL_ENV, "http://broker")
    calls, _timeouts = _patch_client(
        monkeypatch, lambda _url: _response(404), fetch_kind
    )

    first = await _fetch(fetch_kind)
    second = await _fetch(fetch_kind)

    assert first is second
    assert len(calls) == 1


def test_summarise_picks_named_headline_windows():
    providers = {
        "codex": {
            "observed": True,
            "age_seconds": 42,
            "status": "allowed",
            "exhausted": False,
            "windows": [
                {"name": "secondary", "used_percent": 3.5, "resets_at": "c2"},
                {"name": "primary", "used_percent": 24, "resets_at": "c1"},
            ],
        },
        "claude": {
            "observed": True,
            "age_seconds": 3.5,
            "status": "warning",
            "exhausted": False,
            "windows": [
                {"name": "7d", "used_percent": 50, "resets_at": "a2"},
                {"name": "5h", "used_percent": 75, "resets_at": "a1"},
            ],
        },
    }

    result = quota.summarise(providers)

    assert result["codex"]["headline_used_percent"] == 24.0
    assert result["codex"]["headline_window"] == "primary"
    assert result["codex"]["age_seconds"] == 42.0
    assert result["codex"]["resets_at"] == "c1"
    assert result["claude"]["headline_used_percent"] == 75.0
    assert result["claude"]["headline_window"] == "5h"
    assert result["claude"]["age_seconds"] == 3.5
    assert result["claude"]["resets_at"] == "a1"


def test_summarise_falls_back_to_first_active_window():
    providers = {
        "codex": {
            "observed": True,
            "windows": [
                {"name": "primary", "used_percent": 99, "expired": True},
                {"name": "secondary", "used_percent": 8, "resets_at": "later"},
            ],
        }
    }

    result = quota.summarise(providers)

    assert result["codex"]["headline_used_percent"] == 8.0
    assert result["codex"]["headline_window"] == "secondary"
    assert result["codex"]["resets_at"] == "later"


def test_summarise_omits_missing_and_unobserved_providers():
    assert quota.summarise({"claude": {"observed": False}}) == {}


@pytest.mark.parametrize("windows", [[], [{"name": "primary", "expired": True}]])
def test_summarise_returns_none_without_active_windows(windows):
    result = quota.summarise({"codex": {"observed": True, "windows": windows}})

    assert result["codex"]["headline_used_percent"] is None
    assert result["codex"]["headline_window"] is None
    assert result["codex"]["age_seconds"] is None
    assert result["codex"]["resets_at"] is None


def _patch_fetch(monkeypatch, result):
    async def fetch(*, force=False):
        assert force is False
        return result

    monkeypatch.setattr(quota, "fetch_provider_quota", fetch)


@pytest.mark.asyncio
async def test_health_is_ok_when_observed_providers_have_quota(monkeypatch):
    _patch_fetch(
        monkeypatch,
        {
            "available": True,
            "providers": {
                "codex": {
                    "observed": True,
                    "age_seconds": 42,
                    "status": "allowed",
                    "exhausted": False,
                    "windows": [{"name": "primary", "used_percent": 24}],
                },
                "claude": {
                    "observed": True,
                    "age_seconds": 3,
                    "status": "allowed",
                    "exhausted": False,
                    "windows": [{"name": "5h", "used_percent": 75}],
                },
            },
        },
    )

    result = await quota.provider_quota_health()

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["detail"] == (
        "codex 24.0% of primary, observed 42s ago; claude 75.0% of 5h, observed 3s ago"
    )


@pytest.mark.asyncio
async def test_health_labels_fallback_window(monkeypatch):
    _patch_fetch(
        monkeypatch,
        {
            "available": True,
            "providers": {
                "codex": {
                    "observed": True,
                    "age_seconds": 42,
                    "exhausted": False,
                    "windows": [
                        {"name": "primary", "used_percent": 100, "expired": True},
                        {"name": "secondary", "used_percent": 8},
                    ],
                }
            },
        },
    )

    result = await quota.provider_quota_health()

    assert result["detail"] == (
        "codex 8.0% of secondary (primary expired), observed 42s ago"
    )


@pytest.mark.asyncio
async def test_health_is_advisory_and_names_each_exhausted_provider(monkeypatch):
    _patch_fetch(
        monkeypatch,
        {
            "available": True,
            "providers": {
                "codex": {
                    "observed": True,
                    "age_seconds": 42,
                    "exhausted": True,
                    "windows": [
                        {
                            "name": "primary",
                            "used_percent": 100,
                            "resets_at": "2026-09-12T10:07:08Z",
                        }
                    ],
                },
                "claude": {
                    "observed": True,
                    "age_seconds": 3,
                    "exhausted": True,
                    "windows": [
                        {
                            "name": "5h",
                            "used_percent": 95,
                            "resets_at": "2026-09-05T20:00:00Z",
                        }
                    ],
                },
            },
        },
    )

    result = await quota.provider_quota_health()

    assert result["ok"] is False
    assert result["status"] == "advisory"
    assert "codex exhausted at 100.0% (primary), observed 42s ago" in result["detail"]
    assert "2026-09-12T10:07:08Z" in result["detail"]
    assert "claude exhausted at 95.0% (5h), observed 3s ago" in result["detail"]
    assert "2026-09-05T20:00:00Z" in result["detail"]


@pytest.mark.asyncio
async def test_health_is_unknown_when_broker_is_unavailable(monkeypatch):
    _patch_fetch(
        monkeypatch,
        {"available": False, "reason": "broker returned 404", "providers": {}},
    )

    result = await quota.provider_quota_health()

    assert result["ok"] is True
    assert result["status"] == "unknown"
    assert result["detail"] == "broker returned 404"


@pytest.mark.asyncio
async def test_health_is_unknown_when_no_provider_is_observed(monkeypatch):
    _patch_fetch(
        monkeypatch,
        {
            "available": True,
            "providers": {"codex": {"observed": False, "age_seconds": 17}},
        },
    )

    result = await quota.provider_quota_health()

    assert result["ok"] is True
    assert result["status"] == "unknown"
    assert result["detail"] == "no provider quota observed; codex, observed 17s ago"
    assert result["providers"] == {}


def test_unknown_quota_does_not_degrade_aggregate_health(monkeypatch, tmp_path):
    _patch_fetch(
        monkeypatch,
        {"available": False, "reason": "broker unavailable", "providers": {}},
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'quota-health.db'}")
    monkeypatch.setattr("core.db.get_engine", lambda: engine)
    profile = dataclasses.replace(
        PRIVATE_PROFILE,
        mcp_enabled=False,
        otel_enabled=False,
        static_frontend=False,
    )
    module = Module(
        name="quota_test",
        register=lambda _app: None,
        register_health_advisory={"provider_quota": quota.provider_quota_health},
    )

    response = TestClient(build_app(profile, [module])).get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["components"]["provider_quota"]["status"] == "unknown"
    assert "provider_quota" not in body.get("degraded", [])


def test_module_registers_provider_quota_as_advisory():
    assert MODULE.register_health_advisory == {
        "provider_quota": quota.provider_quota_health
    }
