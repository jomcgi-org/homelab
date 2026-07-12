"""Tests for the Semgrep scan webhook (semgrep_scan/perf_webhook.py).

Focus on the security-critical, webhook-specific surface: HMAC verification
(fail-closed) and payload parsing. The downstream capture reuses fetch_scan /
scan_to_row / upsert_scan_perf, which are covered by the harvest + store tests.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from semgrep_scan import perf_webhook

HMAC_KEY = "test-webhook-secret-abcdef"


@pytest.fixture(name="client")
def client_fixture(monkeypatch):
    monkeypatch.setenv("SEMGREP_WEBHOOK_SECRET", HMAC_KEY)
    # Never touch the network / DB: the background capture is stubbed out.
    monkeypatch.setattr(perf_webhook, "_capture_scan", lambda scan_id: None)
    app = FastAPI()
    app.include_router(perf_webhook.router)
    return TestClient(app)


def _sig(body: bytes, secret: str = HMAC_KEY) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_raw_body_signature_is_accepted(client):
    body = json.dumps(
        {"id": 42, "environment": "SCAN_ENVIRONMENT_MANAGED_SCANS"}
    ).encode()
    resp = client.post(
        "/webhooks/semgrep",
        content=body,
        headers={"X-Semgrep-Signature-256": _sig(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted", "scan_id": 42}


def test_valid_canonical_signature_is_accepted(client):
    # Semgrep may sign the compact re-serialization, not the exact bytes sent.
    payload = {"id": 7, "environment": "SCAN_ENVIRONMENT_MANAGED_SCANS"}
    body = json.dumps(payload, indent=2).encode()  # pretty bytes on the wire
    canonical = json.dumps(payload, separators=(",", ":")).encode()
    resp = client.post(
        "/webhooks/semgrep",
        content=body,
        headers={"X-Semgrep-Signature-256": _sig(canonical)},
    )
    assert resp.status_code == 200
    assert resp.json().get("scan_id") == 7


def test_invalid_signature_is_401(client):
    body = json.dumps({"id": 1}).encode()
    resp = client.post(
        "/webhooks/semgrep",
        content=body,
        headers={"X-Semgrep-Signature-256": "deadbeef"},
    )
    assert resp.status_code == 401


def test_missing_signature_is_401(client):
    body = json.dumps({"id": 1}).encode()
    resp = client.post("/webhooks/semgrep", content=body)
    assert resp.status_code == 401


def test_unset_secret_denies_every_request(monkeypatch):
    monkeypatch.delenv("SEMGREP_WEBHOOK_SECRET", raising=False)
    app = FastAPI()
    app.include_router(perf_webhook.router)
    client = TestClient(app)
    body = json.dumps({"id": 1}).encode()
    resp = client.post(
        "/webhooks/semgrep",
        content=body,
        headers={"X-Semgrep-Signature-256": _sig(body)},
    )
    assert resp.status_code == 401


def test_finding_array_payload_is_ignored(client):
    # semgrep_finding events are JSON arrays; only semgrep_scan objects handled.
    body = json.dumps([{"check_id": "x"}]).encode()
    resp = client.post(
        "/webhooks/semgrep",
        content=body,
        headers={"X-Semgrep-Signature-256": _sig(body)},
    )
    assert resp.status_code == 200
    assert resp.json().get("status") == "ignored"


def test_scan_object_without_id_is_ignored(client):
    body = json.dumps({"environment": "SCAN_ENVIRONMENT_MANAGED_SCANS"}).encode()
    resp = client.post(
        "/webhooks/semgrep",
        content=body,
        headers={"X-Semgrep-Signature-256": _sig(body)},
    )
    assert resp.status_code == 200
    assert resp.json().get("status") == "ignored"


def test_managed_row_for_classifies_via_scan_record(monkeypatch):
    # A managed scan record -> a row; a non-managed one -> None (skipped here).
    managed = {
        "id": "500",
        "environment": "SCAN_ENVIRONMENT_MANAGED_SCANS",
        "isFullScan": True,
        "branch": "main",
        "commit": "abc",
        "totalTime": 400.0,
        "findingsCounts": {"total": "3"},
        "cliVersion": "1.168.0",
        "startedAt": None,
        "completedAt": None,
    }
    monkeypatch.setattr(perf_webhook, "_managed_row_for", perf_webhook._managed_row_for)
    from semgrep_scan import perf_harvest

    monkeypatch.setattr(perf_harvest, "fetch_scan", lambda scan_id, token: managed)
    row = perf_webhook._managed_row_for(500)
    assert row is not None
    assert row.environment == "managed-scans"
    assert row.total_time == 400.0

    monkeypatch.setattr(
        perf_harvest,
        "fetch_scan",
        lambda scan_id, token: {
            **managed,
            "environment": "SCAN_ENVIRONMENT_UNSPECIFIED",
        },
    )
    assert perf_webhook._managed_row_for(501) is None


def test_extract_scan_id_tolerates_alias_and_nested_shapes():
    from semgrep_scan.perf_webhook import _extract_scan_id

    assert _extract_scan_id({"id": 5}) == 5
    assert _extract_scan_id({"scan_id": "6"}) == 6
    assert _extract_scan_id({"scan": {"id": 7}}) == 7
    assert _extract_scan_id({"environment": "x"}) is None
