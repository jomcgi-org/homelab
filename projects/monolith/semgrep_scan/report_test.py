"""Tests for the fc-invoke -> Semgrep App relay (semgrep_scan/report.py).

The load-bearing assertion is fidelity: mapping a captured ``semgrep --json``
cli_output back into pysemgrep ``RuleMatch`` objects must reproduce the SAME
``match_based_id`` fingerprint the guest produced, because that fingerprint is
the App's cross-scan dedup / triage-persistence key. The fixtures in
``testdata/`` are a real one-finding cli_output plus the exact rule that produced
it, both captured from the pinned semgrep==1.168.0 Pro engine.

No network happens here: dry_run gates pysemgrep's uploads and we patch the
ScanHandler network methods so start_scan / report_findings / report_failure are
exercised without touching the live App.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from semgrep_scan import report

_TESTDATA = Path(__file__).parent / "testdata"
_CLI_OUTPUT = (_TESTDATA / "pr_cli_output.json").read_text()
_RULES_DIR = str(_TESTDATA)

# The fingerprint the pinned Pro engine emitted for the single finding in the
# captured cli_output. The whole point of the relay is to reproduce this exactly.
_EXPECTED_FINGERPRINT = (
    "98ffefa69953502ca7341c8c4a70f111c67f7d5f44e56be7bfe2bf14d49fda90"
    "3297a99fbae7de52042454a169441e7692db199a45767690cb57d0f304f1f00d_0"
)
_EXPECTED_CHECK_ID = (
    "rules.python.lang.security.dangerous-system-call.dangerous-system-call"
)


def _fake_start_scan(scan_id: int = 424242):
    """A start_scan replacement that sets a scan_response without any network."""

    def _start(self, project_metadata, project_config):
        resp = mock.Mock()
        resp.info.id = scan_id
        self.scan_response = resp

    return _start


def test_mapping_reproduces_fingerprint_and_count():
    filtered, rules_with_matches, unmatched = report.map_cli_output_to_matches(
        _CLI_OUTPUT, rules_dir=_RULES_DIR
    )

    # Exactly the one captured finding maps through, nothing dropped.
    total = sum(len(ms) for ms in filtered.kept.values())
    assert total == 1
    assert unmatched == []
    assert len(rules_with_matches) == 1

    # And its match_based_id is byte-identical to the guest's fingerprint.
    match_ids = [rm.match_based_id for ms in filtered.kept.values() for rm in ms]
    assert match_ids == [_EXPECTED_FINGERPRINT]

    # The rule was renamed to the finding's fully-prefixed check_id so the
    # fingerprint's rule_id component lines up regardless of rules-dir layout.
    rule_ids = [rm.rule_id for ms in filtered.kept.values() for rm in ms]
    assert rule_ids == [_EXPECTED_CHECK_ID]


def test_unmatched_check_id_is_skipped_not_fatal():
    """A finding whose check_id matches no loaded rule is reported as unmatched,
    never crashing the relay."""
    cli = json.loads(_CLI_OUTPUT)
    cli["results"][0]["check_id"] = "totally.unknown.rule.that.is.not.loaded"
    filtered, rules_with_matches, unmatched = report.map_cli_output_to_matches(
        json.dumps(cli), rules_dir=_RULES_DIR
    )
    assert sum(len(ms) for ms in filtered.kept.values()) == 0
    assert len(unmatched) == 1


@pytest.mark.asyncio
async def test_report_pr_scan_dry_run_assembles_payload():
    with mock.patch("semgrep.app.scans.ScanHandler.start_scan", _fake_start_scan(777)):
        result = await report.report_pr_scan(
            repo="jomcgi/homelab",
            branch="feat/test",
            commit="0" * 40,
            pr_id="42",
            base_ref="1" * 40,
            raw_cli_output=_CLI_OUTPUT,
            project_id="3263658",
            repo_url="https://github.com/jomcgi/homelab",
            rules_dir=_RULES_DIR,
            dry_run=True,
        )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["scan_id"] == 777
    assert result["findings_reported"] == 1
    assert result["unmatched_findings"] == 0
    assert result["error"] is None
    # Dry run: no App decision, defaults returned.
    assert result["app_block_override"] is False
    assert result["app_block_reason"] == ""


@pytest.mark.asyncio
async def test_report_pr_scan_closes_scan_on_upload_failure():
    """If report_findings raises after the scan is opened, report_failure MUST be
    called (in the finally) so the App never wedges the PR check on an open
    scan."""
    closed = {}

    def _boom(self, **kwargs):
        raise RuntimeError("upload exploded")

    def _capture_failure(self, exit_code):
        closed["exit_code"] = exit_code

    with (
        mock.patch("semgrep.app.scans.ScanHandler.start_scan", _fake_start_scan(999)),
        mock.patch("semgrep.app.scans.ScanHandler.report_findings", _boom),
        mock.patch("semgrep.app.scans.ScanHandler.report_failure", _capture_failure),
    ):
        result = await report.report_pr_scan(
            repo="jomcgi/homelab",
            branch="feat/test",
            commit="0" * 40,
            pr_id="42",
            raw_cli_output=_CLI_OUTPUT,
            rules_dir=_RULES_DIR,
            dry_run=True,
        )

    assert result["ok"] is False
    assert "upload exploded" in (result["error"] or "")
    # exit_code 2 == internal error, matching the CLI's report_failure contract.
    assert closed.get("exit_code") == 2
