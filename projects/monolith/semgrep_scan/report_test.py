"""Tests for the fc-invoke -> Semgrep App relay (semgrep_scan/report.py).

The load-bearing assertion is fidelity: mapping a captured ``semgrep --json``
cli_output back into pysemgrep ``RuleMatch`` objects must reproduce the SAME
``match_based_id`` fingerprint the guest produced, because that fingerprint is
the App's cross-scan dedup / triage-persistence key. The fixtures in
``testdata/`` are a real one-finding cli_output, the exact rule that produced it,
and the scanned source file, all captured from the pinned semgrep==1.168.0 Pro
engine.

No network happens here. The dry_run path is genuinely network-free (report.py
stubs scan_response instead of calling the real start_scan), so these tests do
NOT monkeypatch start_scan; the failure test patches report_findings to force
the scan-close path. Tests are plain sync functions driving the async relay via
asyncio.run, so no pytest-asyncio plugin is required.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest import mock

import pytest

from semgrep_scan import report

_TESTDATA = Path(__file__).parent / "testdata"
_CLI_OUTPUT = (_TESTDATA / "pr_cli_output.json").read_text()
_RULES_DIR = str(_TESTDATA)

# The scanned source, as the caller would send it to fc-invoke. The finding in
# the captured cli_output is at repo/newfile.py:7, and RuleMatch construction
# reads that line off disk, so the relay must materialize this content.
# newfile.py.src (not *.py, so it is NOT swept into the production :main binary
# where its intentional os.system() would trip the repo's own semgrep scan).
_FILES = [
    {"path": "repo/newfile.py", "content": (_TESTDATA / "newfile.py.src").read_text()}
]

# The fingerprint the pinned Pro engine emitted for the single finding in the
# captured cli_output. The whole point of the relay is to reproduce this exactly.
_EXPECTED_FINGERPRINT = (
    "98ffefa69953502ca7341c8c4a70f111c67f7d5f44e56be7bfe2bf14d49fda90"
    "3297a99fbae7de52042454a169441e7692db199a45767690cb57d0f304f1f00d_0"
)
_EXPECTED_CHECK_ID = (
    "rules.python.lang.security.dangerous-system-call.dangerous-system-call"
)


def test_mapping_reproduces_fingerprint_and_count():
    filtered, rules_with_matches, unmatched = report.map_cli_output_to_matches(
        _CLI_OUTPUT, rules_dir=_RULES_DIR, files=_FILES
    )

    # Exactly the one captured finding maps through, nothing dropped.
    total = sum(len(ms) for ms in filtered.kept.values())
    assert total == 1
    assert unmatched == []
    assert len(rules_with_matches) == 1

    # And its match_based_id is byte-identical to the guest's fingerprint. This
    # is unaffected by materializing the source (fingerprint = formula/path/id).
    match_ids = [rm.match_based_id for ms in filtered.kept.values() for rm in ms]
    assert match_ids == [_EXPECTED_FINGERPRINT]

    # The rule was renamed to the finding's fully-prefixed check_id so the
    # fingerprint's rule_id component lines up regardless of rules-dir layout.
    rule_ids = [rm.rule_id for ms in filtered.kept.values() for rm in ms]
    assert rule_ids == [_EXPECTED_CHECK_ID]


def test_mapping_accepts_dict_cli_output():
    """The real fc-invoke client returns the whole response via resp.json(), so
    raw_cli_output arrives already parsed as a dict (not a str). The mapping must
    accept the dict form and still reproduce the byte-identical fingerprint."""
    cli_dict = json.loads(_CLI_OUTPUT)
    assert isinstance(cli_dict, dict)

    filtered, rules_with_matches, unmatched = report.map_cli_output_to_matches(
        cli_dict, rules_dir=_RULES_DIR, files=_FILES
    )

    total = sum(len(ms) for ms in filtered.kept.values())
    assert total == 1
    assert unmatched == []
    assert len(rules_with_matches) == 1

    match_ids = [rm.match_based_id for ms in filtered.kept.values() for rm in ms]
    assert match_ids == [_EXPECTED_FINGERPRINT]


def test_only_fired_rules_are_loaded_not_whole_dir():
    """Regression guard for the OOM: loading the whole rules dir (~1000+ rules)
    OOM-kills the 1Gi monolith. The mapping must load ONLY the rules whose
    check_id actually fired. We assert the loader is driven by the fired
    check_id set (never asked for everything) and that per-file loading only ever
    touches files, never the whole dir at once."""
    fired = {r["check_id"] for r in json.loads(_CLI_OUTPUT)["results"]}
    assert len(fired) == 1  # the fixture fires exactly one rule

    real_load_fired = report._load_fired_rules
    seen_check_id_sets = []

    def _spy_load_fired(rules_dir, check_ids):
        seen_check_id_sets.append(set(check_ids))
        return real_load_fired(rules_dir, check_ids)

    # And spy the underlying Config.from_config_list so we can prove it is only
    # ever called with a single rule FILE, never the whole rules_dir directory.
    from semgrep.config_resolver import Config

    real_from_config_list = Config.from_config_list
    config_list_args = []

    def _spy_from_config_list(config_list, **kwargs):
        config_list_args.append(list(config_list))
        return real_from_config_list(config_list, **kwargs)

    with (
        mock.patch.object(report, "_load_fired_rules", _spy_load_fired),
        mock.patch.object(
            Config, "from_config_list", staticmethod(_spy_from_config_list)
        ),
    ):
        filtered, rules_with_matches, unmatched = report.map_cli_output_to_matches(
            _CLI_OUTPUT, rules_dir=_RULES_DIR, files=_FILES
        )

    # The loader was asked ONLY for the fired check_ids, never for "everything".
    assert seen_check_id_sets == [fired]

    # Config was never handed the whole rules_dir; every call targets a single
    # rule file (bounded per-file load = bounded memory).
    assert config_list_args, "expected at least one per-file config load"
    for args in config_list_args:
        assert _RULES_DIR not in args
        for entry in args:
            assert os.path.isfile(entry), f"expected a rule file, got {entry!r}"

    # Sanity: the fingerprint still reproduces from the fired-only rule set.
    match_ids = [rm.match_based_id for ms in filtered.kept.values() for rm in ms]
    assert match_ids == [_EXPECTED_FINGERPRINT]
    assert unmatched == []


def test_materialized_sources_restores_cwd():
    """The chdir must be scoped: cwd is restored even after mapping runs."""
    before = Path.cwd()
    report.map_cli_output_to_matches(_CLI_OUTPUT, rules_dir=_RULES_DIR, files=_FILES)
    assert Path.cwd() == before


def test_unmatched_check_id_is_skipped_not_fatal():
    """A finding whose check_id matches no loaded rule is reported as unmatched,
    never crashing the relay."""
    cli = json.loads(_CLI_OUTPUT)
    cli["results"][0]["check_id"] = "totally.unknown.rule.that.is.not.loaded"
    filtered, rules_with_matches, unmatched = report.map_cli_output_to_matches(
        json.dumps(cli), rules_dir=_RULES_DIR, files=_FILES
    )
    assert sum(len(ms) for ms in filtered.kept.values()) == 0
    assert len(unmatched) == 1


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
            files=_FILES,
            project_id="3263658",
            repo_url="https://github.com/jomcgi/homelab",
            rules_dir=_RULES_DIR,
            dry_run=True,
        )
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    # scan_id is null for dry runs (matches the real API), so the stub reports
    # None; the point is the payload assembled with no network and no error.
    assert result["scan_id"] is None
    assert result["findings_reported"] == 1
    assert result["unmatched_findings"] == 0
    assert result["error"] is None
    # Dry run: no App decision, defaults returned.
    assert result["app_block_override"] is False
    assert result["app_block_reason"] == ""


def test_report_pr_scan_closes_scan_on_upload_failure():
    """If report_findings raises after the scan is opened, report_failure MUST be
    called (in the finally) so the App never wedges the PR check on an open
    scan."""
    closed = {}

    def _boom(self, **kwargs):
        raise RuntimeError("upload exploded")

    def _capture_failure(self, exit_code):
        closed["exit_code"] = exit_code

    with (
        mock.patch("semgrep.app.scans.ScanHandler.report_findings", _boom),
        mock.patch("semgrep.app.scans.ScanHandler.report_failure", _capture_failure),
    ):
        result = asyncio.run(
            report.report_pr_scan(
                repo="jomcgi/homelab",
                branch="feat/test",
                commit="0" * 40,
                pr_id="42",
                raw_cli_output=_CLI_OUTPUT,
                files=_FILES,
                rules_dir=_RULES_DIR,
                dry_run=True,
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
