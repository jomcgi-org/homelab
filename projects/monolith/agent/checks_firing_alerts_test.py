"""Pure-HTTP tests for ``agent.checks.check_firing_alerts``.

The function calls SigNoz's ``/api/v1/rules`` endpoint; we mock the
transport with ``httpx.MockTransport`` (the established pattern in this
codebase — see ``home/observability/clickhouse_test.py``).
"""

from __future__ import annotations

import httpx
import pytest

from agent import checks


_RULES_PAYLOAD = {
    "status": "success",
    "data": {
        "rules": [
            {
                "id": "rule-1",
                "alert": "MonolithDown",
                "state": "firing",
                "labels": {"severity": "critical", "team": "platform"},
            },
            {
                "id": "rule-2",
                "alert": "DiskUsageHigh",
                "state": "inactive",
                "labels": {"severity": "warning"},
            },
            {
                "id": "rule-3",
                "alert": "CertExpiringSoon",
                "state": "firing",
                "labels": {"severity": "warning"},
            },
        ]
    },
}


@pytest.mark.asyncio
async def test_returns_only_firing_rules(monkeypatch):
    monkeypatch.setenv("SIGNOZ_URL", "http://signoz.signoz.svc.cluster.local:8080")
    monkeypatch.setenv("SIGNOZ_API_KEY", "test-token")

    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json=_RULES_PAYLOAD)

    transport = httpx.MockTransport(handler)
    result = await checks.check_firing_alerts(transport=transport)

    assert len(result) == 2
    by_id = {row["id"]: row for row in result}
    assert by_id["rule-1"]["name"] == "MonolithDown"
    assert by_id["rule-1"]["state"] == "firing"
    assert by_id["rule-1"]["severity"] == "critical"
    assert by_id["rule-1"]["labels"]["team"] == "platform"
    assert by_id["rule-3"]["severity"] == "warning"

    assert len(seen_requests) == 1
    req = seen_requests[0]
    assert req.url.path == "/api/v1/rules"
    assert req.headers.get("SIGNOZ-API-KEY") == "test-token"


@pytest.mark.asyncio
async def test_empty_when_no_firing_rules(monkeypatch):
    monkeypatch.setenv("SIGNOZ_URL", "http://signoz.signoz.svc.cluster.local:8080")
    monkeypatch.delenv("SIGNOZ_API_KEY", raising=False)

    payload = {
        "status": "success",
        "data": {
            "rules": [
                {
                    "id": "calm-1",
                    "alert": "AllGood",
                    "state": "inactive",
                    "labels": {},
                }
            ]
        },
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))

    result = await checks.check_firing_alerts(transport=transport)

    assert result == []
