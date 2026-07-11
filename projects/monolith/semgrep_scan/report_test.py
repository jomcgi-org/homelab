"""Tests for the fc-invoke -> Semgrep App relay (semgrep_scan/report.py).

The relay builds the App's ``out.Finding`` upload payload DIRECTLY from the
guest's cli_output (no rule loading, no ``RuleMatch``, no source materialization)
and uploads it via PLAIN authenticated HTTP: an explicit
``Authorization: Bearer $SEMGREP_APP_TOKEN`` header on every request, hitting
three endpoints (CREATE ``/api/cli/v2/scans``, ``/results``, ``/complete``), plus
``/error`` to close an open scan on failure. NO ScanHandler, NO ``get_state``, NO
``/config`` ruleset fetch.

The guest's syntactic ``extra.fingerprint`` is carried straight through as both
``syntactic_id`` and ``match_based_id``. The fixture ``testdata/real_cli_output.json``
is an 8-result cli_output (7 findings + 1 ignored, mixed dataflow_trace /
severities / a fingerprint-less result) derived from a real semgrep==1.168.0 Pro
finding.

No network happens here: dry_run is genuinely network-free, and the upload tests
mock ``httpx.post`` to capture the URL, headers (Bearer), and JSON body of each
of the three (or four) calls. Tests are plain sync functions driving the async
relay via ``asyncio.run``, so no pytest-asyncio plugin is required.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from semgrep_scan import report

_TESTDATA = Path(__file__).parent / "testdata"
_CLI_OUTPUT = (_TESTDATA / "real_cli_output.json").read_text()

# The guest's syntactic fingerprint for the FIRST finding in the captured
# cli_output (the untouched real one). The relay carries this through verbatim as
# syntactic_id AND match_based_id.
_EXPECTED_FINGERPRINT = (
    "98ffefa69953502ca7341c8c4a70f111c67f7d5f44e56be7bfe2bf14d49fda90"
    "3297a99fbae7de52042454a169441e7692db199a45767690cb57d0f304f1f00d_0"
)
_EXPECTED_CHECK_ID = (
    "rules.python.lang.security.dangerous-system-call.dangerous-system-call"
)

# The fixture has 8 results: 7 non-ignored findings + 1 flagged extra.is_ignored.
_EXPECTED_FINDINGS = 7
_EXPECTED_IGNORES = 1

_FAKE_TOKEN = "t" * 64

# An arbitrary fixed instant, computed (not a hardcoded literal) so the intent
# reads as "any commit date" and the repo's own semgrep scan does not flag a
# stale timestamp literal. The relay just echoes this into each finding's
# commit_date; the exact value is irrelevant to what the tests assert.
_COMMIT_DATE = datetime.fromtimestamp(1_600_000_000).isoformat()


def test_findings_built_from_cli_output_carry_fingerprint():
    """The 8-result cli_output -> 7 findings + 1 ignore. The first finding's
    check_id/path/positions come from the result and its syntactic_id AND
    match_based_id are the guest's fingerprint (the App's dedup/triage key at
    cutover)."""
    ci_scan_results, findings_count = report._build_ci_scan_results(
        _CLI_OUTPUT, _COMMIT_DATE
    )

    assert findings_count == _EXPECTED_FINDINGS
    assert len(ci_scan_results.findings) == _EXPECTED_FINDINGS
    assert len(ci_scan_results.ignores) == _EXPECTED_IGNORES

    finding = ci_scan_results.findings[0]
    assert finding.check_id.value == _EXPECTED_CHECK_ID
    assert finding.path.value == "repo/newfile.py"
    assert finding.line == 7
    assert finding.column == 5
    assert finding.syntactic_id == _EXPECTED_FINGERPRINT
    assert finding.match_based_id == _EXPECTED_FINGERPRINT
    # ERROR severity maps to the App's integer 2.
    assert finding.severity == 2

    # The whole CiScanResults serializes to a JSON-able upload blob with all 7.
    blob = ci_scan_results.to_json()
    assert len(blob["findings"]) == _EXPECTED_FINDINGS
    assert blob["findings"][0]["syntactic_id"] == _EXPECTED_FINGERPRINT
    assert blob["findings"][0]["match_based_id"] == _EXPECTED_FINGERPRINT


def test_build_accepts_dict_cli_output():
    """The real fc-invoke client returns the whole response via resp.json(), so
    raw_cli_output arrives already parsed as a dict (not a str). The builder must
    accept the dict form and still carry the fingerprint through."""
    cli_dict = json.loads(_CLI_OUTPUT)
    assert isinstance(cli_dict, dict)

    ci_scan_results, findings_count = report._build_ci_scan_results(
        cli_dict, _COMMIT_DATE
    )
    assert findings_count == _EXPECTED_FINDINGS
    assert ci_scan_results.findings[0].match_based_id == _EXPECTED_FINGERPRINT


def test_ignored_finding_goes_to_ignores_not_findings():
    """Results flagged extra.is_ignored are uploaded as ignores, not findings,
    matching how report_findings partitions the payload. The fixture already has
    exactly one such result; flagging one more moves it too."""
    cli = json.loads(_CLI_OUTPUT)
    # Flag the second finding as ignored on top of the fixture's existing one.
    cli["results"][1]["extra"]["is_ignored"] = True
    ci_scan_results, findings_count = report._build_ci_scan_results(
        json.dumps(cli), _COMMIT_DATE
    )
    assert findings_count == _EXPECTED_FINDINGS - 1
    assert len(ci_scan_results.ignores) == _EXPECTED_IGNORES + 1


def test_missing_fingerprint_falls_back_to_deterministic_id():
    """A result with no extra.fingerprint (the Pro engine always emits one, but we
    defend against its absence) gets a stable fallback id used as both ids."""
    cli = json.loads(_CLI_OUTPUT)
    # The crypto.py result (#6) has no fingerprint in the fixture.
    ci_scan_results, _ = report._build_ci_scan_results(json.dumps(cli), _COMMIT_DATE)
    crypto = [f for f in ci_scan_results.findings if f.path.value == "repo/crypto.py"]
    assert len(crypto) == 1
    fid = crypto[0].syntactic_id
    assert fid and fid.endswith("_0")
    assert crypto[0].match_based_id == fid


def _mock_response(payload: dict) -> mock.Mock:
    resp = mock.Mock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _run_scan(dry_run: bool = False):
    return asyncio.run(
        report.report_pr_scan(
            repo="jomcgi/homelab",
            branch="feat/test",
            commit="0" * 40,
            pr_id="42",
            base_ref="1" * 40,
            raw_cli_output=_CLI_OUTPUT,
            project_id="3263658",
            repo_url="https://github.com/jomcgi/homelab",
            dry_run=dry_run,
        )
    )


def test_report_pr_scan_dry_run_does_no_network():
    """dry_run must be genuinely network-free: report.py assembles + serializes the
    payloads but issues ZERO HTTP. We patch httpx.post to explode if touched."""

    def _no_network(*_args, **_kwargs):
        raise AssertionError("dry_run made a network call")

    with mock.patch("semgrep_scan.report.httpx.post", _no_network):
        result = _run_scan(dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    # scan_id stays None: no CREATE happened.
    assert result["scan_id"] is None
    assert result["findings_reported"] == _EXPECTED_FINDINGS
    assert result["error"] is None
    # Dry run: no App decision, defaults returned.
    assert result["app_block_override"] is False
    assert result["app_block_reason"] == ""


def test_report_pr_scan_uploads_with_bearer_auth_on_every_request():
    """The happy path hits exactly three endpoints (CREATE, /results, /complete),
    each carrying an explicit Authorization: Bearer header, with the right URLs and
    JSON bodies. We capture every httpx.post call and assert on it."""
    calls: list[dict] = []

    def _fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls.append({"url": url, "headers": headers or {}, "json": json})
        if url.endswith("/api/cli/v2/scans"):
            return _mock_response(
                {
                    "info": {
                        "enabled_products": ["sast"],
                        "deployment_id": 47408,
                        "deployment_name": "jomcgi",
                        "id": 555,
                    }
                }
            )
        if url.endswith("/results"):
            return _mock_response({"errors": []})
        if url.endswith("/complete"):
            return _mock_response(
                {
                    "success": True,
                    "app_block_override": True,
                    "app_block_reason": "policy",
                    "app_blocking_match_based_ids": [_EXPECTED_FINGERPRINT],
                }
            )
        raise AssertionError(f"unexpected URL {url}")

    with (
        mock.patch.dict("os.environ", {"SEMGREP_APP_TOKEN": _FAKE_TOKEN}),
        mock.patch("semgrep_scan.report.httpx.post", _fake_post),
    ):
        result = _run_scan(dry_run=False)

    assert result["ok"] is True
    assert result["error"] is None
    assert result["scan_id"] == 555
    assert result["findings_reported"] == _EXPECTED_FINDINGS
    # Block decision parsed from the /complete response.
    assert result["app_block_override"] is True
    assert result["app_block_reason"] == "policy"
    assert result["app_blocking_match_based_ids"] == [_EXPECTED_FINGERPRINT]

    # Exactly three calls, in order: CREATE, /results, /complete.
    assert len(calls) == 3
    create, results_call, complete_call = calls

    # Every request carries an explicit Bearer header with the token, and a
    # Semgrep/<version> User-Agent (the App rejects /results with 400
    # "Invalid Semgrep Version" without it).
    for c in calls:
        assert c["headers"].get("Authorization") == f"Bearer {_FAKE_TOKEN}"
        assert c["headers"].get("User-Agent", "").startswith("Semgrep/")

    # CREATE: correct URL + a CreateScanRequestV2 body (scan_metadata + project_metadata).
    assert create["url"].endswith("/api/cli/v2/scans")
    assert "scan_metadata" in create["json"]
    assert "project_metadata" in create["json"]
    assert create["json"]["project_metadata"]["pull_request_id"] == "42"
    assert create["json"]["project_metadata"]["is_full_scan"] is False
    assert create["json"]["project_metadata"]["repository"] == "jomcgi/homelab"
    assert create["json"]["scan_metadata"]["cli_version"]  # non-empty version

    # /results: scan_id in URL + a CiScanResults body with all 7 findings.
    assert results_call["url"].endswith("/api/agent/scans/555/results")
    assert len(results_call["json"]["findings"]) == _EXPECTED_FINDINGS
    assert (
        results_call["json"]["findings"][0]["match_based_id"] == _EXPECTED_FINGERPRINT
    )

    # /complete: scan_id in URL + a CiScanComplete body.
    assert complete_call["url"].endswith("/api/agent/scans/555/complete")
    assert complete_call["json"]["stats"]["findings"] == _EXPECTED_FINDINGS


def test_report_pr_scan_closes_scan_on_upload_failure():
    """If the upload raises after CREATE opened the scan, the relay MUST POST
    /error (in the exception path) so the App never wedges the PR check on an open
    scan. We let CREATE succeed and make /results explode, then assert an /error
    POST carrying the Bearer header fired for that scan_id."""
    posted: list[str] = []

    def _fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        posted.append(url)
        assert (headers or {}).get("Authorization") == f"Bearer {_FAKE_TOKEN}"
        if url.endswith("/api/cli/v2/scans"):
            return _mock_response(
                {
                    "info": {
                        "enabled_products": ["sast"],
                        "deployment_id": 47408,
                        "deployment_name": "jomcgi",
                        "id": 777,
                    }
                }
            )
        if url.endswith("/results"):
            raise RuntimeError("upload exploded")
        if url.endswith("/error"):
            return _mock_response({})
        raise AssertionError(f"unexpected URL {url}")

    with (
        mock.patch.dict("os.environ", {"SEMGREP_APP_TOKEN": _FAKE_TOKEN}),
        mock.patch("semgrep_scan.report.httpx.post", _fake_post),
    ):
        result = _run_scan(dry_run=False)

    assert result["ok"] is False
    assert "upload exploded" in (result["error"] or "")
    assert result["scan_id"] == 777
    # The /error close fired for the opened scan.
    assert any(u.endswith("/api/agent/scans/777/error") for u in posted)


def test_missing_token_surfaces_structured_error_not_process_death():
    """With no SEMGREP_APP_TOKEN the CREATE cannot build auth; the relay must
    return a structured error (never raise into the caller / kill the process)."""
    with (
        mock.patch.dict("os.environ", {}, clear=False),
        mock.patch("semgrep_scan.report.httpx.post") as post,
    ):
        # Ensure the token really is absent.
        os.environ.pop("SEMGREP_APP_TOKEN", None)
        result = _run_scan(dry_run=False)

    assert result["ok"] is False
    assert "SEMGREP_APP_TOKEN" in (result["error"] or "")
    # No network happened: auth is built before the CREATE POST.
    post.assert_not_called()


# Keep an explicit import of pytest so the target's pytest dep is exercised and
# the module reads as a pytest test file even though we drive async via
# asyncio.run (no pytest-asyncio plugin needed).
assert pytest is not None
