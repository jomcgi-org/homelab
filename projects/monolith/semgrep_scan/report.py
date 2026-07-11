"""Relay fc-invoke Semgrep findings to the Semgrep AppSec Platform.

The fc-invoke semgrep guest scans a PR on our own Firecracker VM and returns the
verbatim ``semgrep --json`` cli_output (``raw_cli_output``, see the guest's
``ScanResult.RawCliOutput``). This module turns that cli_output DIRECTLY into the
App's ``out.Finding`` upload payload and POSTs it under a per-PR scan using
pysemgrep's OWN client (``semgrep.app.scans.ScanHandler`` for ``start_scan`` and
the authenticated ``app_session``), so the Semgrep App applies policy/triage
server-side and posts the native PR check exactly as a Managed Scan would. The
guest itself stays air-gapped from the App; the monolith owns the whole App
conversation.

WHY NO RULE LOADING (the pivot):
    The previous design recomputed Semgrep's ``match_based_id`` by reconstructing
    ``RuleMatch`` objects, which required loading the rule definitions the guest
    scanned with. The Pro packs are monolithic YAML (~480MB each), and loading
    them OOM-killed the 1Gi monolith. We ABANDONED that. The cli_output already
    carries everything the App needs: every ``results[]`` entry has ``check_id``,
    ``path``, ``start``/``end`` positions, and an ``extra`` block with
    ``message``, ``severity``, ``metadata``, ``fingerprint``, ``engine_kind``,
    ``validation_state`` and ``dataflow_trace``. We build ``out.Finding`` objects
    straight from those fields. NO rule loading, NO ``RuleMatch``, NO
    ``config_resolver.Config``, NO source-file materialization.

    Accepted trade: ``extra.fingerprint`` is the guest's SYNTACTIC fingerprint,
    which we use as both ``syntactic_id`` and ``match_based_id``. This is NOT the
    same key the App historically stored (that was the interfile ``match_based_id``
    the Pro engine computes over rule formulae), so triage state resets ONCE at
    cutover. That is acceptable and intended.

VERSION COUPLING (read before bumping semgrep):
    This module still depends on pysemgrep INTERNAL APIs: the ``out.*`` atd types
    from ``semgrep_output_v1``, ``ScanHandler`` (for ``start_scan`` +
    ``report_failure``), and the authenticated ``state.app_session``. The pin is
    ``semgrep==1.168.0`` in ``bazel/requirements/tools.in``. A bump REQUIRES
    re-verifying the ``out.Finding`` / ``out.CiScanResults`` / ``out.CiScanComplete``
    field surface and the two POST endpoints below (``/results`` + ``/complete``,
    replicated from ``ScanHandler.report_findings``).

THE UPLOAD (spike result):
    ``ScanHandler.report_findings`` builds ``out.CiScanResults{findings, ignores,
    token=None, searched_paths, renamed_paths, rule_ids, contributions}`` from
    ``match.to_app_finding_format(...)`` and POSTs it to
    ``{semgrep_url}/api/agent/scans/{scan_id}/results``, then POSTs an
    ``out.CiScanComplete`` to ``.../complete`` and reads the block decision from
    the ``CiScanCompleteResponse``. We build the ``out.Finding`` list ourselves
    (no ``RuleMatch``), assemble the same ``CiScanResults`` + ``CiScanComplete``,
    and replicate those two POSTs via ``state.app_session`` (which carries the
    ``SEMGREP_APP_TOKEN`` auth). ``start_scan`` needs NO rules: it just registers
    the scan and returns the deployment config + scan_id.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Optional

logger = logging.getLogger("monolith.semgrep.report")

# The scan_environment string the App tags our scans with. Distinguishes Route B
# (self-hosted) scans from Semgrep Managed Scans in the dashboard.
SCAN_ENVIRONMENT = "homelab-fc-invoke"

# cli_output severity string -> App integer severity, following the same mapping
# pysemgrep's ``RuleMatch.to_app_finding_format`` uses (Critical=3, Error/High=2,
# Warning/Medium=1, Experiment=4, everything else=0).
_APP_SEVERITY = {
    "CRITICAL": 3,
    "ERROR": 2,
    "HIGH": 2,
    "WARNING": 1,
    "MEDIUM": 1,
    "EXPERIMENT": 4,
}


_semgrep_home_ready = False


def _ensure_writable_semgrep_home() -> None:
    """Point pysemgrep at a writable HOME + data/log/settings dirs.

    We no longer load rules, but we still call ``ScanHandler.start_scan`` and use
    the authenticated ``state.app_session``. Building ``SemgrepState`` (via
    ``get_state()``, which ``ScanHandler.__init__`` calls) constructs ``Settings``,
    whose ``__attrs_post_init__`` does ``self.save()`` -> ``self.path.parent.mkdir``
    under ``Path.home()/.semgrep`` (or ``$SEMGREP_SETTINGS_FILE``'s parent). The
    monolith runs non-root with ``HOME=/`` (unwritable), so that mkdir raises
    ``[Errno 30] Read-only file system: '/.semgrep'`` without this safeguard
    (verified against the pinned 1.168.0 in the spike).

    We mirror the guest's ``HOME=/tmp/sghome`` pattern: if ``HOME`` is missing or
    not writable, point it (and the specific semgrep data-dir env vars) at a
    writable temp dir. Idempotent: safe to call at every relay entry.

    The App token is read from ``SEMGREP_APP_TOKEN`` directly, not from any of
    these files.
    """
    global _semgrep_home_ready
    if _semgrep_home_ready:
        return

    home = os.environ.get("HOME")
    if not (home and os.path.isdir(home) and os.access(home, os.W_OK)):
        writable_home = tempfile.mkdtemp(prefix="semgrep-relay-home-")
        os.environ["HOME"] = writable_home
        home = writable_home

    semgrep_data = os.path.join(home, ".semgrep")
    os.makedirs(semgrep_data, exist_ok=True)
    os.environ.setdefault("XDG_CONFIG_HOME", home)
    os.environ.setdefault("XDG_CACHE_HOME", home)
    os.environ.setdefault("SEMGREP_LOG_FILE", os.path.join(semgrep_data, "semgrep.log"))

    if not os.environ.get("SEMGREP_SETTINGS_FILE"):
        os.environ["SEMGREP_SETTINGS_FILE"] = os.path.join(
            semgrep_data, "semgrep-relay-settings.yml"
        )

    _semgrep_home_ready = True


def _finding_from_cli_result(
    result: dict[str, Any], index: int, commit_date_iso: str
) -> Any:
    """Build one ``out.Finding`` straight from a cli_output ``results[]`` entry.

    Field mapping (spike result), against the atd-generated ``out.Finding``:

    REQUIRED (no default in out.Finding):
        check_id     <- result["check_id"]              (out.RuleId)
        path         <- result["path"]                  (out.Fpath)
        line/column  <- result["start"]["line"/"col"]
        end_line/col <- result["end"]["line"/"col"]
        message      <- extra["message"]
        severity     <- _APP_SEVERITY[extra["severity"]] (int)
        index        <- per-(rule,path,code) dedup index we assign
        commit_date  <- head-commit epoch, iso-formatted
        syntactic_id <- extra["fingerprint"]            (guest syntactic fp)
        metadata     <- out.RawJson(extra["metadata"])
        is_blocking  <- True (App policy decides real blocking server-side)

    OPTIONAL:
        match_based_id  <- extra["fingerprint"] (same fp; App dedup/triage key)
        dataflow_trace  <- extra["dataflow_trace"] if present
        validation_state<- extra["validation_state"] if present
        engine_kind     <- extra["engine_kind"] if present
        hashes/fixed_lines/sca_info/historical_info are left None: they are
        location/code hashes we cannot cheaply reproduce without the source, and
        the App tolerates their absence (they are Optional in out.Finding).
    """
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out

    extra = result.get("extra", {})
    fingerprint = extra.get("fingerprint") or _fallback_fingerprint(result)

    app_severity = _APP_SEVERITY.get(str(extra.get("severity", "")).upper(), 0)

    engine_kind = (
        out.EngineOfFinding.from_json(extra["engine_kind"])
        if extra.get("engine_kind") is not None
        else None
    )
    validation_state = (
        out.ValidationState.from_json(extra["validation_state"])
        if extra.get("validation_state") is not None
        else None
    )
    dataflow_trace = (
        out.MatchDataflowTrace.from_json(extra["dataflow_trace"])
        if extra.get("dataflow_trace") is not None
        else None
    )

    start = result.get("start", {})
    end = result.get("end", {})
    return out.Finding(
        check_id=out.RuleId(result["check_id"]),
        path=out.Fpath(result["path"]),
        line=start.get("line", 0),
        column=start.get("col", 0),
        end_line=end.get("line", 0),
        end_column=end.get("col", 0),
        message=extra.get("message", ""),
        severity=app_severity,
        index=index,
        commit_date=commit_date_iso,
        syntactic_id=fingerprint,
        match_based_id=fingerprint,
        metadata=out.RawJson(extra.get("metadata") or {}),
        is_blocking=True,
        hashes=None,
        dataflow_trace=dataflow_trace,
        validation_state=validation_state,
        engine_kind=engine_kind,
    )


def _fallback_fingerprint(result: dict[str, Any]) -> str:
    """Deterministic id when a cli result carries no ``extra.fingerprint``.

    The Pro engine always emits a fingerprint, so this is a defensive fallback
    only: a stable hash of ``check_id + path + start`` so the same finding gets
    the same id across scans even without a guest fingerprint.
    """
    import hashlib

    start = result.get("start", {})
    key = "{}:{}:{}:{}".format(
        result.get("check_id", ""),
        result.get("path", ""),
        start.get("line", 0),
        start.get("col", 0),
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest() + "_0"


def _build_ci_scan_results(
    raw_cli_output: dict[str, Any] | str,
    commit_date_iso: str,
) -> tuple[Any, int]:
    """Map a ``semgrep --json`` cli_output to an ``out.CiScanResults`` payload.

    ``raw_cli_output`` may be EITHER the already-parsed cli_output ``dict`` (the
    real fc-invoke client shape: the client does ``resp.json()`` so the embedded
    JSON object arrives already parsed) OR the raw JSON ``str`` (the test-fixture
    shape). Both are accepted.

    Splits results into findings vs ignores by ``extra.is_ignored`` (matching how
    ``report_findings`` partitions), assigns each finding a per-(check_id, path,
    line) dedup index, and assembles the ``CiScanResults`` that goes to
    ``/results``. Returns ``(ci_scan_results, findings_count)``.
    """
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out

    cli = (
        raw_cli_output
        if isinstance(raw_cli_output, dict)
        else json.loads(raw_cli_output)
    )
    results = cli.get("results", [])

    findings: list[Any] = []
    ignores: list[Any] = []
    rule_ids: set[str] = set()
    searched: set[str] = set()
    # Per-(check_id, path, code-position) counter so duplicated findings get a
    # distinct index, mirroring pysemgrep's finding-index behaviour.
    index_counts: dict[tuple[str, str, int, int], int] = {}

    for result in results:
        check_id = result.get("check_id", "")
        path = result.get("path", "")
        start = result.get("start", {})
        key = (check_id, path, start.get("line", 0), start.get("col", 0))
        idx = index_counts.get(key, 0)
        index_counts[key] = idx + 1

        finding = _finding_from_cli_result(result, idx, commit_date_iso)
        if result.get("extra", {}).get("is_ignored"):
            ignores.append(finding)
        else:
            findings.append(finding)
        rule_ids.add(check_id)
        searched.add(path)

    ci_scan_results = out.CiScanResults(
        token=None,
        findings=findings,
        ignores=ignores,
        searched_paths=[out.Fpath(p) for p in sorted(searched)],
        renamed_paths=[],
        skipped_paths=[],
        rule_ids=[out.RuleId(rid) for rid in sorted(rule_ids)],
        contributions=out.Contributions([]),
    )
    return ci_scan_results, len(findings)


def _build_ci_scan_complete(findings_count: int) -> Any:
    """Assemble the ``out.CiScanComplete`` payload POSTed to ``/complete``.

    Only the fields the App needs to finalize a diff scan are populated; error
    lists and parse-rate stats are empty (we did not run the parser here, the
    guest did). ``exit_code`` is the CLI hint (non-zero when there are findings);
    the App applies its own policy to decide blocking.
    """
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out

    return out.CiScanComplete(
        exit_code=1 if findings_count else 0,
        dependencies=out.CiScanDependencies(value={}),
        dependency_parser_errors=[],
        stats=out.CiScanCompleteStats(
            findings=findings_count,
            errors=[],
            total_time=0.0,
            unsupported_exts={},
            lockfile_scan_info={},
            parse_rate={},
            engine_requested="PRO_INTERFILE",
            findings_by_product={"code": findings_count},
        ),
    )


def _build_project_metadata(
    *,
    repo: str,
    branch: str,
    commit: str,
    pr_id: str,
    base_ref: Optional[str],
    project_id: Optional[str],
    repo_url: Optional[str],
) -> Any:
    """Construct ``out.ProjectMetadata`` for a PR diff scan.

    We build it directly (rather than via ``semgrep.meta`` git helpers) because
    the monolith is not running inside the scanned git checkout; all the PR facts
    come from the webhook payload the caller passes in.
    """
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out

    payload: dict[str, Any] = {
        "scan_environment": SCAN_ENVIRONMENT,
        "repository": repo,
        "repo_url": repo_url,
        "branch": branch,
        "commit": commit,
        "commit_title": None,
        "commit_author_email": None,
        "commit_author_name": None,
        "commit_author_username": None,
        "commit_author_image_url": None,
        "ci_job_url": None,
        "on": "pull_request",
        "pull_request_author_username": None,
        "pull_request_author_image_url": None,
        "pull_request_id": pr_id,
        "pull_request_title": None,
        "is_full_scan": False,
    }
    if base_ref:
        # Lets the App compute the merge base for server-side baseline handling.
        payload["base_branch_head_commit"] = base_ref
    if project_id:
        payload["project_id"] = project_id
    return out.ProjectMetadata.from_json(payload)


class _DryRunScanResponse:
    """A stand-in for ScanHandler.scan_response used only in dry_run.

    pysemgrep's ``ScanHandler.start_scan`` POSTs to semgrep.dev with NO dry_run
    guard, and on a bad token it calls ``sys.exit`` (a BaseException). So dry_run
    cannot rely on calling the real ``start_scan``. Instead we set
    ``scan_response`` to this stub so ``scan_id`` resolves without any network.
    """

    class _Info:
        id = None  # scan id is null for dry runs, matching the real API

    info = _Info()


def _post_results_and_complete(
    handler: Any,
    ci_scan_results: Any,
    complete: Any,
) -> Any:
    """Replicate ``report_findings``' two POSTs, minus the RuleMatch mapping.

    POST the assembled ``CiScanResults`` to ``/results`` then the
    ``CiScanComplete`` to ``/complete`` via the authenticated ``app_session``,
    and return the parsed ``out.CiScanCompleteResponse``. This is the exact
    request/response shape ``ScanHandler.report_findings`` uses (see the pinned
    1.168.0 ``app/scans.py``): same URLs, same JSON bodies, same auth session.
    """
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out
    from semgrep.state import get_state

    state = get_state()
    base = f"{state.env.semgrep_url}/api/agent/scans/{handler.scan_id}"

    results_resp = state.app_session.post(
        f"{base}/results",
        timeout=state.env.upload_findings_timeout,
        json=ci_scan_results.to_json(),
    )
    results_resp.raise_for_status()

    complete_resp = state.app_session.post(
        f"{base}/complete",
        timeout=state.env.upload_findings_timeout,
        json=complete.to_json(),
    )
    complete_resp.raise_for_status()
    return out.CiScanCompleteResponse.from_json(complete_resp.json())


async def report_pr_scan(
    *,
    repo: str,
    branch: str,
    commit: str,
    pr_id: str,
    base_ref: Optional[str] = None,
    raw_cli_output: dict[str, Any] | str,
    project_id: Optional[str] = None,
    repo_url: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Report an fc-invoke PR scan to the Semgrep App, built from cli_output.

    Opens a per-PR scan (``start_scan``, which needs NO rules), maps
    ``raw_cli_output`` DIRECTLY into ``out.Finding`` objects (no ``RuleMatch``, no
    rule loading), POSTs them (``/results`` + ``/complete`` via the authenticated
    ``app_session``), and returns the App's block decision. ``raw_cli_output`` may
    be the already-parsed cli_output ``dict`` (the real fc-invoke client shape) or
    the raw JSON ``str``.

    On ANY failure after the scan is opened it calls ``report_failure`` in a
    ``finally`` so the App never wedges the PR check on an open scan.
    ``SEMGREP_APP_TOKEN`` is read from the environment by pysemgrep; this module
    never hardcodes it.

    With ``dry_run=True`` NO network happens at all: we skip the real
    ``start_scan`` (which POSTs with no dry_run guard) by stubbing
    ``scan_response``, and we skip the ``/results`` + ``/complete`` POSTs. Used by
    tests and any caller that wants to assemble the payload without touching the
    live App.
    """
    _ensure_writable_semgrep_home()

    # Imports are function-local so importing this module does not eagerly pull
    # the heavy pysemgrep app/state machinery unless a scan is actually reported.
    from semgrep.app.project_config import ProjectConfig
    from semgrep.app.scans import ScanHandler

    result: dict[str, Any] = {
        "ok": False,
        "dry_run": dry_run,
        "scan_id": None,
        "findings_reported": 0,
        "app_block_override": False,
        "app_block_reason": "",
        "app_blocking_match_based_ids": [],
        "error": None,
    }

    commit_date_iso = datetime.fromtimestamp(int(time.time())).isoformat()

    try:
        ci_scan_results, findings_count = _build_ci_scan_results(
            raw_cli_output, commit_date_iso
        )
    except Exception as exc:  # noqa: BLE001 - surface mapping failure as structured
        logger.exception("failed to build CiScanResults from cli_output")
        result["error"] = f"mapping failed: {exc}"
        return result

    result["findings_reported"] = findings_count
    complete = _build_ci_scan_complete(findings_count)

    project_metadata = _build_project_metadata(
        repo=repo,
        branch=branch,
        commit=commit,
        pr_id=pr_id,
        base_ref=base_ref,
        project_id=project_id,
        repo_url=repo_url,
    )

    handler = ScanHandler(enable_transitive_reachability=None, dry_run=dry_run)

    scan_opened = False
    try:
        if dry_run:
            # Do NOT call the real start_scan: it POSTs to semgrep.dev with no
            # dry_run guard and sys.exit()s on a bad token. Stub the response so
            # scan_id resolves with zero network.
            handler.scan_response = _DryRunScanResponse()  # type: ignore[assignment]
        else:
            handler.start_scan(project_metadata, ProjectConfig())
        scan_opened = True
        result["scan_id"] = handler.scan_id

        if dry_run:
            # Assemble-only: serialize both payloads so the dry run proves they
            # are well-formed, but do NO network.
            ci_scan_results.to_json()
            complete.to_json()
            result["ok"] = True
            return result

        complete_response = _post_results_and_complete(
            handler, ci_scan_results, complete
        )
        result["ok"] = True
        result["app_block_override"] = complete_response.app_block_override
        result["app_block_reason"] = complete_response.app_block_reason
        result["app_blocking_match_based_ids"] = [
            mid.value for mid in complete_response.app_blocking_match_based_ids
        ]
        return result
    except BaseException as exc:  # noqa: BLE001 - see below; must catch SystemExit
        # Catch BaseException, not Exception: a bad SEMGREP_APP_TOKEN makes
        # pysemgrep's start_scan call sys.exit(INVALID_API_KEY_EXIT_CODE), which
        # raises SystemExit (a BaseException). Without this the relay would kill
        # the whole monolith process on a token problem instead of returning a
        # structured error. Re-raise genuine interrupts so we don't swallow them.
        if isinstance(exc, KeyboardInterrupt):
            raise
        logger.exception("report_pr_scan failed after start_scan=%s", scan_opened)
        result["error"] = str(exc) or type(exc).__name__
        if scan_opened:
            try:
                # Close the open scan so the App does not leave the PR check
                # spinning. Exit code 2 = internal error, mirroring the CLI.
                handler.report_failure(2)
            except Exception:  # noqa: BLE001 - best-effort close
                logger.exception("report_failure also failed; scan may be left open")
        return result
