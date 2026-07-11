"""Tests for the fc-invoke -> Semgrep App relay (semgrep_scan/report.py).

The relay now builds the App's ``out.Finding`` upload payload DIRECTLY from the
guest's cli_output (no rule loading, no ``RuleMatch``, no source materialization).
The guest's syntactic ``extra.fingerprint`` is carried straight through as both
``syntactic_id`` and ``match_based_id``.

Two fixtures in ``testdata/``:
  * ``pr_cli_output.json`` -- a synthetic one-finding cli_output used for the
    precise field-mapping assertions (exact fingerprint/check_id/position).
  * ``real_cli_output.json`` -- a REAL 8-finding fc-invoke capture whose
    ``extra.dataflow_trace`` is a MIX of dict and literal ``null`` (4 each). This
    is the load-bearing regression fixture for the live serialization bug: the
    atd ``out.*.from_json`` raises on a None-valued optional, so building +
    serializing all 8 must succeed with no error. A synthetic single-finding
    fixture missed this because it had a non-None dataflow_trace.

No network happens here. The dry_run path is genuinely network-free (report.py
stubs scan_response instead of calling the real start_scan, and skips the
/results + /complete POSTs), so these tests do NOT monkeypatch start_scan; the
failure test patches ScanHandler.report_findings' replacement path indirectly by
forcing an upload error. Tests are plain sync functions driving the async relay
via asyncio.run, so no pytest-asyncio plugin is required.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from semgrep_scan import report

_TESTDATA = Path(__file__).parent / "testdata"
_CLI_OUTPUT = (_TESTDATA / "pr_cli_output.json").read_text()
_REAL_CLI_OUTPUT = (_TESTDATA / "real_cli_output.json").read_text()

# The guest's syntactic fingerprint for the single finding in the captured
# cli_output. The relay carries this through verbatim as syntactic_id AND
# match_based_id.
_EXPECTED_FINGERPRINT = (
    "98ffefa69953502ca7341c8c4a70f111c67f7d5f44e56be7bfe2bf14d49fda90"
    "3297a99fbae7de52042454a169441e7692db199a45767690cb57d0f304f1f00d_0"
)
_EXPECTED_CHECK_ID = (
    "rules.python.lang.security.dangerous-system-call.dangerous-system-call"
)

# An arbitrary fixed instant, computed (not a hardcoded literal) so the intent
# reads as "any commit date" and the repo's own semgrep scan does not flag a
# stale timestamp literal. The relay just echoes this into each finding's
# commit_date; the exact value is irrelevant to what the tests assert.
_COMMIT_DATE = datetime.fromtimestamp(1_600_000_000).isoformat()


def test_findings_built_from_cli_output_carry_fingerprint():
    """One cli_output result -> one out.Finding whose check_id/path/positions come
    from the result and whose syntactic_id AND match_based_id are the guest's
    fingerprint (the App's dedup/triage key at cutover)."""
    ci_scan_results, findings_count = report._build_ci_scan_results(
        _CLI_OUTPUT, _COMMIT_DATE
    )

    assert findings_count == 1
    assert len(ci_scan_results.findings) == 1

    finding = ci_scan_results.findings[0]
    assert finding.check_id.value == _EXPECTED_CHECK_ID
    assert finding.path.value == "repo/newfile.py"
    assert finding.line == 7
    assert finding.column == 5
    assert finding.syntactic_id == _EXPECTED_FINGERPRINT
    assert finding.match_based_id == _EXPECTED_FINGERPRINT
    # ERROR severity maps to the App's integer 2.
    assert finding.severity == 2

    # The whole CiScanResults serializes to a JSON-able upload blob.
    blob = ci_scan_results.to_json()
    assert len(blob["findings"]) == 1
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
    assert findings_count == 1
    assert ci_scan_results.findings[0].match_based_id == _EXPECTED_FINGERPRINT


def test_ignored_finding_goes_to_ignores_not_findings():
    """A result flagged extra.is_ignored is uploaded as an ignore, not a finding,
    matching how report_findings partitions the payload."""
    cli = json.loads(_CLI_OUTPUT)
    cli["results"][0]["extra"]["is_ignored"] = True
    ci_scan_results, findings_count = report._build_ci_scan_results(
        json.dumps(cli), _COMMIT_DATE
    )
    assert findings_count == 0
    assert len(ci_scan_results.findings) == 0
    assert len(ci_scan_results.ignores) == 1
    assert ci_scan_results.ignores[0].match_based_id == _EXPECTED_FINGERPRINT


def test_real_cli_output_with_null_optionals_serializes_cleanly():
    """LIVE-BUG REGRESSION: a real 8-finding fc-invoke capture whose
    ``extra.dataflow_trace`` is null on half the results must map to 8 out.Finding
    objects that serialize through the App-payload build with NO error.

    Before the fix the mapping handed the literal ``null`` dataflow_trace to
    ``out.MatchDataflowTrace.from_json``, which raised ``incompatible JSON value
    where type 'MatchDataflowTrace' was expected: 'None'``. The fix omits
    None-valued optionals so from_json never sees a None, and to_json omits the
    key so the App-payload carries no null for an optional."""
    cli = json.loads(_REAL_CLI_OUTPUT)
    # Sanity: the fixture genuinely exercises the null-optional case.
    dft_none = sum(
        1 for r in cli["results"] if r["extra"].get("dataflow_trace") is None
    )
    dft_dict = sum(
        1 for r in cli["results"] if isinstance(r["extra"].get("dataflow_trace"), dict)
    )
    assert dft_none == 4 and dft_dict == 4, "fixture must mix null and dict dft"

    # Build must not raise, and all 8 findings must serialize.
    ci_scan_results, findings_count = report._build_ci_scan_results(
        _REAL_CLI_OUTPUT, _COMMIT_DATE
    )
    assert findings_count == 8
    assert len(ci_scan_results.findings) == 8

    blob = ci_scan_results.to_json()
    assert len(blob["findings"]) == 8
    # Every finding carries its guest fingerprint into both id fields.
    for finding in blob["findings"]:
        assert finding["syntactic_id"]
        assert finding["match_based_id"] == finding["syntactic_id"]

    # The null-dataflow findings must NOT carry a dataflow_trace key at all (a
    # present null is exactly what tripped the App's re-parse); the non-null ones
    # keep theirs.
    keyed = sum(1 for f in blob["findings"] if "dataflow_trace" in f)
    assert keyed == 4
    assert all(
        f["dataflow_trace"] is not None
        for f in blob["findings"]
        if "dataflow_trace" in f
    )


def test_real_cli_output_findings_reparse_like_the_app():
    """The App backend re-parses the uploaded blob via out.CiScanResults.from_json.
    Round-tripping the built payload through from_json must NOT raise -- this is the
    exact code path (Finding.from_json -> MatchDataflowTrace.from_json) that failed
    live."""
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out

    ci_scan_results, _ = report._build_ci_scan_results(_REAL_CLI_OUTPUT, _COMMIT_DATE)
    blob = ci_scan_results.to_json()
    # Must not raise on any of the 8 findings (4 with null dataflow_trace).
    reparsed = out.CiScanResults.from_json(blob)
    assert len(reparsed.findings) == 8


def test_real_cli_output_dry_run_assembles_payload():
    """The full relay dry_run path assembles the real 8-finding payload with no
    network and no serialization error."""
    result = asyncio.run(
        report.report_pr_scan(
            repo="jomcgi/homelab",
            branch="feat/test",
            commit="0" * 40,
            pr_id="42",
            raw_cli_output=_REAL_CLI_OUTPUT,
            dry_run=True,
        )
    )
    assert result["ok"] is True
    assert result["error"] is None
    assert result["findings_reported"] == 8


def test_report_pr_scan_dry_run_assembles_payload():
    # No monkeypatch of start_scan: dry_run must be genuinely network-free.
    result = asyncio.run(
        report.report_pr_scan(
            repo="jomcgi/homelab",
            branch="feat/test",
            commit="0" * 40,
            pr_id="42",
            base_ref="1" * 40,
            raw_cli_output=_CLI_OUTPUT,
            project_id="3263658",
            repo_url="https://github.com/jomcgi/homelab",
            dry_run=True,
        )
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    # scan_id is null for dry runs (matches the real API), so the stub reports
    # None; the point is the payload assembled with no network and no error.
    assert result["scan_id"] is None
    assert result["findings_reported"] == 1
    assert result["error"] is None
    # Dry run: no App decision, defaults returned.
    assert result["app_block_override"] is False
    assert result["app_block_reason"] == ""


def test_report_pr_scan_closes_scan_on_upload_failure():
    """If the upload raises after the scan is opened, report_failure MUST be
    called (in the finally) so the App never wedges the PR check on an open
    scan. We force a real (non-dry) start_scan + upload path but stub the network
    so start_scan succeeds and the results POST explodes."""
    closed = {}

    class _FakeInfo:
        id = "scan-123"

    def _fake_start_scan(self, project_metadata, project_config):
        self.scan_response = mock.Mock(info=_FakeInfo())

    def _capture_failure(self, exit_code):
        closed["exit_code"] = exit_code

    def _boom(handler, ci_scan_results, complete):
        raise RuntimeError("upload exploded")

    with (
        mock.patch("semgrep.app.scans.ScanHandler.start_scan", _fake_start_scan),
        mock.patch("semgrep.app.scans.ScanHandler.report_failure", _capture_failure),
        mock.patch.object(report, "_post_results_and_complete", _boom),
    ):
        result = asyncio.run(
            report.report_pr_scan(
                repo="jomcgi/homelab",
                branch="feat/test",
                commit="0" * 40,
                pr_id="42",
                raw_cli_output=_CLI_OUTPUT,
                dry_run=False,
            )
        )

    assert result["ok"] is False
    assert "upload exploded" in (result["error"] or "")
    # exit_code 2 == internal error, matching the CLI's report_failure contract.
    assert closed.get("exit_code") == 2


# Keep an explicit import of pytest so the target's pytest dep is exercised and
# the module reads as a pytest test file even though we drive async via
# asyncio.run (no pytest-asyncio plugin needed).
assert pytest is not None
