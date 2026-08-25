"""Tests for the ember-durability health component (#4338, ADR embervm/031)."""

from __future__ import annotations

import json
import httpx
import pytest

from ember_public import durability


HEALTHY_PAYLOAD = {
    "ok": True,
    "evaluated_at_unix_ms": 1756000000000,
    "tier1": {
        "ok": True,
        "verdict": "ok",
        "detail": "all tracked artifact kinds have confirmed store copies",
        "streaks": {"session": 0},
        "failing_kinds": [],
        "fresh_nodes": ["node-1"],
        "missing_nodes": [],
    },
    "tier2": {
        "ok": True,
        "verdict": "ok",
        "detail": "newest gc-manifests object is fresh",
        "newest_manifest_age_ms": 3600000,
        "stall_bound_ms": 90000000,
    },
}


def _transport(
    status_code: int = 200, body: bytes | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            content=body if body is not None else json.dumps(HEALTHY_PAYLOAD).encode(),
            headers={"content-type": "application/json"},
            request=request,
        )

    return httpx.MockTransport(handler)


def _run(check):
    import asyncio

    return asyncio.run(check())


# -- factory / dark landing -----------------------------------------------------


def test_factory_returns_none_while_unconfigured() -> None:
    assert durability.build_durability_health("") is None
    assert durability.build_durability_health(None) is None


def test_configured_url_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(durability.ENV_URL, raising=False)
    assert durability.configured_url() == ""
    monkeypatch.setenv(durability.ENV_URL, "http://embervm:8080/v1/health/durability")
    assert durability.configured_url() == "http://embervm:8080/v1/health/durability"


def test_module_registration_stays_dark_without_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Importing the module must not blow up and, with no URL configured, must
    # not add an ember-durability component at all.
    monkeypatch.delenv(durability.ENV_URL, raising=False)
    from ember_public.module import MODULE  # noqa: PLC0415

    assert MODULE.register_health is not None
    assert "ember-durability" not in MODULE.register_health


def test_module_registration_appears_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(durability.ENV_URL, "http://embervm:8080/v1/health/durability")
    import importlib

    from ember_public import module as module_mod  # noqa: PLC0415

    importlib.reload(module_mod)
    try:
        assert module_mod.MODULE.register_health is not None
        check = module_mod.MODULE.register_health.get("ember-durability")
        assert check is not None
        # It must be a FATAL-tier registration (register_health), never in the
        # advisory map: an advisory signal pages nobody, and both tiers end in
        # the health surface.
        assert module_mod.MODULE.register_health_advisory in (None, {})
    finally:
        monkeypatch.delenv(durability.ENV_URL, raising=False)
        importlib.reload(module_mod)


# -- the check itself -------------------------------------------------------------


def test_healthy_payload_reads_ok() -> None:
    check = durability.build_durability_health(
        "http://x/durability", transport=_transport()
    )
    result = _run(check)
    assert result == {
        "ok": True,
        "detail": "durability ok (tier1=ok, tier2=ok)",
    }


def test_explicit_not_ok_reads_fatal_with_tier_detail() -> None:
    payload = {
        "ok": False,
        "tier1": {
            "ok": False,
            "verdict": "export_failure_streak",
            "detail": "exports failing for session",
        },
        "tier2": {"ok": True, "verdict": "ok", "detail": "fresh"},
    }
    check = durability.build_durability_health(
        "http://x/durability", transport=_transport(body=json.dumps(payload).encode())
    )
    result = _run(check)
    assert result["ok"] is False
    assert "tier1=export_failure_streak" in result["detail"]
    assert "exports failing" in result["detail"]


def test_http_503_from_the_cp_never_reads_ok_and_keeps_tier_detail() -> None:
    # The CP answers 503 WITH the report body when a tier is not ok; the
    # component must read not-ok AND surface which tier and why.
    payload = {
        "ok": False,
        "tier1": {
            "ok": False,
            "verdict": "gc_sweep_stalled",
            "detail": "newest gc-manifests object is too old",
        },
        "tier2": {"ok": False, "verdict": "gc_sweep_stalled", "detail": "old"},
    }
    check = durability.build_durability_health(
        "http://x/durability",
        transport=_transport(status_code=503, body=json.dumps(payload).encode()),
    )
    result = _run(check)
    assert result["ok"] is False
    assert "HTTP 503" in result["detail"]
    assert "gc_sweep_stalled" in result["detail"]


def test_http_404_dark_cp_reads_not_ok_with_hint() -> None:
    check = durability.build_durability_health(
        "http://x/durability",
        transport=_transport(status_code=404, body=b'{"error": "not found"}'),
    )
    result = _run(check)
    assert result["ok"] is False
    assert "HTTP 404" in result["detail"]
    assert "not found" in result["detail"]


def test_missing_ok_field_never_reads_ok() -> None:
    # The vacuous-green guard: a 200 body without an explicit ok:true (a shape
    # drift, an empty object, a proxy's default page) reads NOT ok.
    for body in [b"{}", b"<html>proxy error</html>", b"null"]:
        check = durability.build_durability_health(
            "http://x/durability", transport=_transport(body=body)
        )
        result = _run(check)
        assert result["ok"] is False, body


def test_transport_error_never_reads_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    check = durability.build_durability_health(
        "http://x/durability", transport=httpx.MockTransport(handler)
    )
    result = _run(check)
    assert result["ok"] is False
    assert "unreachable" in result["detail"]


def test_tier_missing_from_payload_reads_not_ok() -> None:
    payload = {"ok": True, "tier1": HEALTHY_PAYLOAD["tier1"]}
    check = durability.build_durability_health(
        "http://x/durability", transport=_transport(body=json.dumps(payload).encode())
    )
    result = _run(check)
    assert result["ok"] is False
    assert "tier2=missing" in result["detail"]
