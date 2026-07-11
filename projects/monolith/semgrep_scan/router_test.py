"""Tests for the GitHub PR webhook (semgrep_scan/router.py, Phase 2).

Covers the security surface and the happy-path dispatch, all hermetic (no live
GitHub, no fc-invoke, no Semgrep App):

- HMAC verify, fail-closed: a valid signature is accepted; a bad, missing, or
  unsigned request is 401; an UNSET secret is 401 (never accept).
- Event filter: a non-``pull_request`` event (and a ``ping``) is a 200 no-op; an
  unsupported ``action`` is a 200 no-op. Neither dispatches the scan.
- Dispatch path: a valid ``opened`` PR event runs the background job, which lists
  the PR's changed files, fetches only the scannable ones at the head sha, calls
  ``scan_files`` then ``report_pr_scan``. The GitHub API (httpx), ``scan_files``,
  and ``report_pr_scan`` are all mocked; we assert ``report_pr_scan`` is called
  with the right PR metadata and that only scannable extensions are scanned.

FastAPI's TestClient runs BackgroundTasks synchronously after the response, so
asserting on the mocks right after the POST returns exercises the background job.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest import mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from semgrep_scan import router as webhook

_SECRET = "test-webhook-secret"


@pytest.fixture
def client(monkeypatch):
    """A TestClient over just the webhook router with the HMAC secret set."""
    monkeypatch.setenv("GITHUB_SEMGREP_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setenv("GITHUB_API_TOKEN", "gh-test-token")
    app = FastAPI()
    app.include_router(webhook.router)
    return TestClient(app)


def _sign(body: bytes, secret: str = _SECRET) -> str:
    """Compute the ``sha256=<hex>`` header GitHub would send for ``body``."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _pr_payload(action: str = "opened") -> dict:
    return {
        "action": action,
        "number": 4242,
        "repository": {
            "full_name": "jomcgi/homelab",
            "html_url": "https://github.com/jomcgi/homelab",
        },
        "pull_request": {
            "number": 4242,
            "head": {"ref": "feat/thing", "sha": "headsha123"},
            "base": {"ref": "main", "sha": "basesha456"},
        },
    }


def _post(client, payload, *, event="pull_request", secret=_SECRET, sign=True):
    body = json.dumps(payload).encode("utf-8")
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if sign:
        headers["X-Hub-Signature-256"] = _sign(body, secret)
    return client.post("/webhooks/github/semgrep", content=body, headers=headers)


# --- HMAC verification (fail-closed) ---------------------------------------


def test_valid_signature_accepted_and_dispatches(client):
    with (
        mock.patch.object(
            webhook, "_gather_files", new=mock.AsyncMock(return_value=[])
        ),
        mock.patch.object(webhook, "scan_files", new=mock.AsyncMock()) as scan,
    ):
        resp = _post(client, _pr_payload("opened"))
    assert resp.status_code == 200
    assert resp.json().get("status") == "accepted"
    # No files gathered -> early return, scan never runs (nothing to scan).
    scan.assert_not_awaited()


def test_bad_signature_rejected(client):
    resp = _post(client, _pr_payload(), secret="wrong-secret")
    assert resp.status_code == 401


def test_missing_signature_rejected(client):
    resp = _post(client, _pr_payload(), sign=False)
    assert resp.status_code == 401


def test_unset_secret_rejected(client, monkeypatch):
    # An unset secret must deny every request, even a well-formed one.
    monkeypatch.delenv("GITHUB_SEMGREP_WEBHOOK_SECRET", raising=False)
    resp = _post(client, _pr_payload())
    assert resp.status_code == 401


# --- Event / action filter (200 no-op, no dispatch) ------------------------


def test_ping_event_is_noop(client):
    with mock.patch.object(webhook, "_scan_and_report") as job:
        resp = _post(client, {"zen": "hi"}, event="ping")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ignored"
    job.assert_not_called()


def test_non_pull_request_event_is_noop(client):
    with mock.patch.object(webhook, "_scan_and_report") as job:
        resp = _post(client, {"ref": "refs/heads/main"}, event="push")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ignored"
    job.assert_not_called()


def test_unsupported_action_is_noop(client):
    with mock.patch.object(webhook, "_scan_and_report") as job:
        resp = _post(client, _pr_payload("closed"))
    assert resp.status_code == 200
    assert resp.json().get("status") == "ignored"
    job.assert_not_called()


# --- Changed-files fetch -> scan -> report (all mocked) --------------------


def _fake_github_client(files_pages, contents):
    """Build a stand-in httpx.AsyncClient whose .get answers the two endpoints.

    ``files_pages`` is the list returned by GET .../pulls/{n}/files (single page);
    ``contents`` maps a path to its decoded text (base64-encoded in the response).
    """
    import base64

    class _Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            if url.endswith("/files"):
                # Page 1 returns the files; any later page returns empty (stop).
                page = (params or {}).get("page", 1)
                return _Resp(files_pages if page == 1 else [])
            # contents endpoint: /repos/{repo}/contents/{path}
            path = url.split("/contents/", 1)[1]
            text = contents[path]
            return _Resp(
                {
                    "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                    "encoding": "base64",
                }
            )

    return _Client()


def test_scan_and_report_happy_path(client):
    files_pages = [
        {"filename": "app/main.py", "status": "modified"},
        {"filename": "svc/handler.go", "status": "added"},
        {"filename": "README.md", "status": "modified"},  # unscannable ext
        {"filename": "old/gone.py", "status": "removed"},  # deleted -> skip
    ]
    contents = {
        "app/main.py": "print('hi')\n",
        "svc/handler.go": "package main\n",
    }
    scan_result = {"raw_cli_output": {"results": []}}
    report_result = {"ok": True, "scan_id": 99, "findings_reported": 0}

    with (
        mock.patch.object(
            webhook.httpx,
            "AsyncClient",
            return_value=_fake_github_client(files_pages, contents),
        ),
        mock.patch.object(
            webhook, "scan_files", new=mock.AsyncMock(return_value=scan_result)
        ) as scan,
        mock.patch.object(
            webhook,
            "report_pr_scan",
            new=mock.AsyncMock(return_value=report_result),
        ) as report,
    ):
        resp = _post(client, _pr_payload("synchronize"))
        assert resp.status_code == 200
        assert resp.json().get("status") == "accepted"

    # Only scannable, non-removed files were scanned (README skipped, deleted .py
    # skipped), each with its fetched content.
    scan.assert_awaited_once()
    scanned = scan.await_args.args[0]
    scanned_paths = sorted(f["path"] for f in scanned)
    assert scanned_paths == ["app/main.py", "svc/handler.go"]
    assert all("content" in f for f in scanned)

    # report_pr_scan gets the PR metadata from the payload.
    report.assert_awaited_once()
    kwargs = report.await_args.kwargs
    assert kwargs["repo"] == "jomcgi/homelab"
    assert kwargs["branch"] == "feat/thing"
    assert kwargs["commit"] == "headsha123"
    assert kwargs["pr_id"] == "4242"
    assert kwargs["base_ref"] == "basesha456"
    assert kwargs["raw_cli_output"] == {"results": []}


def test_scan_error_short_circuits_report(client):
    files_pages = [{"filename": "app/main.py", "status": "modified"}]
    contents = {"app/main.py": "print('hi')\n"}

    with (
        mock.patch.object(
            webhook.httpx,
            "AsyncClient",
            return_value=_fake_github_client(files_pages, contents),
        ),
        mock.patch.object(
            webhook,
            "scan_files",
            new=mock.AsyncMock(return_value={"error": "fc-invoke down"}),
        ),
        mock.patch.object(webhook, "report_pr_scan", new=mock.AsyncMock()) as report,
    ):
        resp = _post(client, _pr_payload("opened"))
        assert resp.status_code == 200

    # A scan error must not reach report_pr_scan.
    report.assert_not_awaited()


def test_no_scannable_files_skips_scan(client):
    files_pages = [{"filename": "docs/README.md", "status": "modified"}]

    with (
        mock.patch.object(
            webhook.httpx,
            "AsyncClient",
            return_value=_fake_github_client(files_pages, {}),
        ),
        mock.patch.object(webhook, "scan_files", new=mock.AsyncMock()) as scan,
        mock.patch.object(webhook, "report_pr_scan", new=mock.AsyncMock()) as report,
    ):
        resp = _post(client, _pr_payload("opened"))
        assert resp.status_code == 200

    scan.assert_not_awaited()
    report.assert_not_awaited()
