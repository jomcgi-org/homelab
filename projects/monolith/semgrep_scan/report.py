"""Relay fc-invoke Semgrep findings to the Semgrep AppSec Platform.

The fc-invoke semgrep guest scans a PR on our own Firecracker VM and returns the
verbatim ``semgrep --json`` cli_output (``raw_cli_output``, see the guest's
``ScanResult.RawCliOutput``). This module turns that cli_output DIRECTLY into the
App's ``out.Finding`` upload payload and POSTs it under a per-PR scan using PLAIN
authenticated HTTP (``httpx`` with an explicit ``Authorization: Bearer`` from
``SEMGREP_APP_TOKEN``), so the Semgrep App applies policy/triage server-side and
posts the native PR check exactly as a Managed Scan would. The guest itself stays
air-gapped from the App; the monolith owns the whole App conversation.

WHY NOT ScanHandler (the second pivot, from live debugging against deployment
47408 / org jomcgi on semgrep.dev):
    ``semgrep.app.scans.ScanHandler`` is the WRONG tool in a long-lived non-CLI
    process, for two confirmed reasons:

    1. Auth never attaches. ScanHandler's internal ``get_state()`` calls resolve
       ``ctx.ensure_object(SemgrepState)`` from the click context. OUTSIDE a click
       command every such call builds a FRESH context, hence a fresh
       unauthenticated ``AppSession`` (token=None), so its POSTs go out with no
       ``Authorization`` header and the App returns
       ``401 {"error":"Invalid Authorization"}``. ``app_session.authenticate()``
       only helps within one click scope, which ScanHandler's own ``get_state()``
       calls do not share.
    2. Config download OOMs. Even when auth succeeds, ``start_scan`` (v2) polls
       ``GET /api/cli/v2/scans/{id}/config``, which returns the deployment's FULL
       ruleset; parsing it OOM-kills the 1Gi monolith (exit 137). We do NOT scan,
       so we must NEVER fetch that config.

    The token itself is VALID: a plain ``Authorization: Bearer $SEMGREP_APP_TOKEN``
    to ``GET /api/agent/deployments/current`` returns 200. So we bypass ScanHandler
    entirely and speak plain authenticated HTTP to exactly three endpoints.

WHY NO RULE LOADING (the first pivot):
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

MEMORY DISCIPLINE:
    We import ONLY the atd-generated ``out.*`` types from ``semgrep_output_v1``
    plus ``__VERSION__`` and ``httpx``. We NEVER import ``semgrep.state`` or
    ``semgrep.app.scans`` (those pull the heavy Terminal/Settings machinery and,
    for ScanHandler, the OOM config poll). We NEVER fetch or parse the deployment
    ruleset. No rules live anywhere in this module.

VERSION COUPLING (read before bumping semgrep):
    This module still depends on pysemgrep's ``out.*`` atd types from
    ``semgrep_output_v1`` and on the three App endpoints below. The pin is
    ``semgrep==1.168.0`` in ``bazel/requirements/tools.in``. A bump REQUIRES
    re-verifying the ``out.Finding`` / ``out.CiScanResults`` / ``out.CiScanComplete``
    / ``out.CreateScanRequestV2`` / ``out.CreateScanResponseV2`` field surface and
    the three endpoint shapes.

THE THREE-ENDPOINT FLOW (verified against pinned 1.168.0 ``app/scans.py``):
    1. CREATE  POST ``{url}/api/cli/v2/scans`` with an ``out.CreateScanRequestV2``
       ``{scan_metadata, project_metadata, project_config=None}``. The response is
       an ``out.CreateScanResponseV2`` whose ``info.id`` is the ``scan_id`` and is
       returned IMMEDIATELY by the POST (``start_scan_v2`` docstring: "POST ... to
       create scan (returns scan info immediately)"). We read ``scan_id`` from THIS
       response and NEVER poll ``/config`` (that is the OOM path).
    2. RESULTS POST ``{url}/api/agent/scans/{scan_id}/results`` with the
       ``out.CiScanResults`` we built from cli_output (``.to_json()``).
    3. COMPLETE POST ``{url}/api/agent/scans/{scan_id}/complete`` with the
       ``out.CiScanComplete`` (``.to_json()``); the response parses to an
       ``out.CiScanCompleteResponse`` carrying the block decision.
    On any failure after CREATE succeeded we POST
    ``{url}/api/agent/scans/{scan_id}/error`` with ``{"exit_code", "stderr"}`` (the
    exact shape ``ScanHandler.report_failure`` uses) so the App never wedges the
    PR check on an open scan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from typing import Optional

import httpx

logger = logging.getLogger("monolith.semgrep.report")

# The scan_environment string the App tags our scans with. Distinguishes Route B
# (self-hosted) scans from Semgrep Managed Scans in the dashboard.
SCAN_ENVIRONMENT = "homelab-fc-invoke"

# Semgrep App base URL. semgrep.dev is the SaaS default; overridable via env for a
# self-hosted App without touching the heavy pysemgrep state machinery.
_DEFAULT_SEMGREP_URL = "https://semgrep.dev"

# Seconds. The results/complete POSTs can be slow on the App side; keep a generous
# but bounded timeout so a hung App never blocks the monolith request forever.
_UPLOAD_TIMEOUT = 60

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


def _semgrep_url() -> str:
    """Resolve the Semgrep App base URL WITHOUT triggering pysemgrep state.

    Reads ``SEMGREP_URL`` / ``SEMGREP_APP_URL`` (either name, in that order) if set,
    otherwise the semgrep.dev default. We deliberately do NOT read
    ``state.env.semgrep_url`` because building ``SemgrepState`` pulls in the heavy
    Terminal/Settings machinery we are avoiding.
    """
    return (
        os.environ.get("SEMGREP_URL")
        or os.environ.get("SEMGREP_APP_URL")
        or _DEFAULT_SEMGREP_URL
    ).rstrip("/")


def _auth_headers() -> dict[str, str]:
    """Build the explicit Bearer auth header from ``SEMGREP_APP_TOKEN``.

    This is the whole fix: every outbound request carries the token explicitly,
    instead of relying on ScanHandler's ``get_state().app_session`` (which is
    unauthenticated outside a click command). Raises if the token is missing so the
    failure is a clear structured error, not a silent 401.
    """
    token = os.environ.get("SEMGREP_APP_TOKEN")
    if not token:
        raise RuntimeError(
            "SEMGREP_APP_TOKEN is not set; cannot authenticate to the Semgrep App"
        )
    from semgrep import __VERSION__

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        # The App parses the semgrep version from the User-Agent and rejects the
        # /results POST with 400 {"error":"Invalid Semgrep Version"} without it.
        # This matches str(semgrep.app.session.UserAgent()) == "Semgrep/<ver>".
        "User-Agent": f"Semgrep/{__VERSION__}",
    }


def _decode_optional(out_type: Any, value: Any) -> Any:
    """Decode an optional nested ``out.*`` field, safe against None.

    Returns ``out_type.from_json(value)`` ONLY when ``value`` is not None; returns
    None otherwise. The atd-generated ``from_json`` raises on a None value (it
    reads a PRESENT key as "must be a valid value of this type"), so a cli_output
    optional emitted as literal ``null`` MUST NOT be handed to ``from_json``. This
    is the single guard that keeps every optional-field decode None-safe.
    """
    if value is None:
        return None
    return out_type.from_json(value)


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

    OPTIONAL (OMITTED, i.e. left None on the Finding, when the cli value is
    None/absent):
        match_based_id  <- extra["fingerprint"] (same fp; App dedup/triage key)
        dataflow_trace  <- extra["dataflow_trace"] when present and non-None
        validation_state<- extra["validation_state"] when present and non-None
        engine_kind     <- extra["engine_kind"] when present and non-None
        hashes/fixed_lines/sca_info/historical_info are left None: they are
        location/code hashes we cannot cheaply reproduce without the source, and
        the App tolerates their absence (they are Optional in out.Finding).

    CRITICAL (live bug, ENGINE-fingerprint capture): the cli_output emits these
    optional keys as literal ``null`` for many findings (e.g. ``dataflow_trace``
    is null on ~half the results in a real diff scan). The atd-generated
    ``out.*.from_json`` treats a value of ``None`` as "invalid value for this
    type" and RAISES (``incompatible JSON value where type 'MatchDataflowTrace'
    was expected: 'None'``), NOT as "absent". So we must NEVER call the nested
    ``from_json`` on a None value: we decode each optional ONLY when its source is
    present and non-None, and otherwise leave the field None on the Finding.
    ``out.Finding.to_json`` then omits the key entirely (verified against the
    pinned 1.168.0 generated ``to_json``), so the App-payload never carries a null
    for an optional and the App's own re-parse cannot trip the same error.
    """
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out

    extra = result.get("extra", {})
    fingerprint = extra.get("fingerprint") or _fallback_fingerprint(result)

    app_severity = _APP_SEVERITY.get(str(extra.get("severity", "")).upper(), 0)

    engine_kind = _decode_optional(out.EngineOfFinding, extra.get("engine_kind"))
    validation_state = _decode_optional(
        out.ValidationState, extra.get("validation_state")
    )
    dataflow_trace = _decode_optional(
        out.MatchDataflowTrace, extra.get("dataflow_trace")
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


def _build_ci_scan_complete(
    findings_count: int, scan_execution_duration: Optional[float] = None
) -> Any:
    """Assemble the ``out.CiScanComplete`` payload POSTed to ``/complete``.

    Only the fields the App needs to finalize a diff scan are populated; error
    lists and parse-rate stats are empty (we did not run the parser here, the
    guest did). ``exit_code`` is the CLI hint (non-zero when there are findings);
    the App applies its own policy to decide blocking.

    ``scan_execution_duration`` (seconds) is the engine scan time measured by the
    webhook around the fc-invoke call; it populates ``stats.total_time`` so the
    App's scan-time column reflects the real engine duration instead of a
    hardcoded 0. ``None`` (e.g. dry-run callers that never timed a scan) falls
    back to 0.0.
    """
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out

    return out.CiScanComplete(
        exit_code=1 if findings_count else 0,
        dependencies=out.CiScanDependencies(value={}),
        dependency_parser_errors=[],
        stats=out.CiScanCompleteStats(
            findings=findings_count,
            errors=[],
            total_time=float(scan_execution_duration or 0.0),
            unsupported_exts={},
            lockfile_scan_info={},
            parse_rate={},
            engine_requested="PRO_INTERFILE",
            findings_by_product={"code": findings_count},
        ),
    )


def _build_scan_metadata() -> Any:
    """Build the light ``out.ScanMetadata`` for the CREATE request.

    Mirrors ``ScanHandler.__init__`` (cli_version + a client-generated unique_id +
    empty requested_products), but WITHOUT ``get_state()``: the ``unique_id`` is a
    fresh uuid we mint ourselves rather than ``state.local_scan_id``, and
    ``dry_run`` is always False here (dry_run never reaches the network so it never
    builds a CREATE request). No rules, no state.
    """
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out
    from semgrep import __VERSION__

    return out.ScanMetadata(
        cli_version=out.Version(__VERSION__),
        unique_id=out.Uuid(str(uuid.uuid4())),
        requested_products=[],
        dry_run=False,
    )


def _reported_repository(repo: str) -> str:
    """Resolve the ``repository`` name the App scan lands under.

    When ``SEMGREP_SHADOW_PROJECT`` is set and non-empty we report under that
    SHADOW project name (e.g. ``jomcgi/homelab-selfhosted``) instead of the real
    ``repo``, so Route B (self-hosted) scans land in a SEPARATE Semgrep project
    from SMS's ``jomcgi/homelab`` project and the two can be compared side by
    side. Unset (the cutover state) falls back to the real ``repo`` so Route B
    reports to the real project with no code change.
    """
    shadow = os.environ.get("SEMGREP_SHADOW_PROJECT")
    if shadow and shadow.strip():
        return shadow.strip()
    return repo


def _build_project_metadata(
    *,
    repo: str,
    branch: str,
    commit: str,
    pr_id: Optional[str],
    base_ref: Optional[str],
    project_id: Optional[str],
    repo_url: Optional[str],
    is_full_scan: bool = False,
) -> Any:
    """Construct ``out.ProjectMetadata`` for a PR diff scan or a whole-repo FULL scan.

    We build it directly (rather than via ``semgrep.meta`` git helpers) because
    the monolith is not running inside the scanned git checkout; all the PR facts
    come from the webhook payload the caller passes in.

    The ``repository`` value comes from ``_reported_repository(repo)``: with
    ``SEMGREP_SHADOW_PROJECT`` set it is the shadow project name, not the real
    ``repo``, so these scans land in a separate Semgrep project for comparison.

    ``is_full_scan=False`` (the default) is byte-identical to the original PR
    diff scan behavior: ``"on": "pull_request"`` and ``pull_request_id`` set to
    ``pr_id``. ``is_full_scan=True`` is a whole-repo interfile scan (the
    ``semgrep-full`` workload seeding the baseline on ``main``): the payload sets
    ``"is_full_scan": True`` and ``"on": "schedule"``. ``pull_request_id`` is a
    REQUIRED field in the ProjectMetadata ATD schema (``from_json`` raises
    "missing field 'pull_request_id'" if absent), so a full scan still sends it,
    as an empty string, rather than omitting it.
    """
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out

    payload: dict[str, Any] = {
        "scan_environment": SCAN_ENVIRONMENT,
        "repository": _reported_repository(repo),
        "repo_url": repo_url,
        "branch": branch,
        "commit": commit,
        "commit_title": None,
        "commit_author_email": None,
        "commit_author_name": None,
        "commit_author_username": None,
        "commit_author_image_url": None,
        "ci_job_url": None,
        "on": "schedule" if is_full_scan else "pull_request",
        "pull_request_author_username": None,
        "pull_request_author_image_url": None,
        "pull_request_title": None,
        "is_full_scan": is_full_scan,
        # Required field in the ProjectMetadata ATD schema. A full scan has no PR,
        # so it sends an empty string; a PR diff scan sends the real pr_id.
        "pull_request_id": "" if is_full_scan else pr_id,
    }
    if base_ref:
        # Lets the App compute the merge base for server-side baseline handling.
        payload["base_branch_head_commit"] = base_ref
    if project_id:
        payload["project_id"] = project_id
    return out.ProjectMetadata.from_json(payload)


def _create_scan(scan_metadata: Any, project_metadata: Any) -> Optional[int]:
    """CREATE the scan via plain authenticated HTTP and return the ``scan_id``.

    POSTs an ``out.CreateScanRequestV2`` to ``/api/cli/v2/scans`` with an explicit
    ``Authorization: Bearer`` header. The pinned ``start_scan_v2`` confirms the
    POST response ``CreateScanResponseV2.info.id`` IS the scan_id and is returned
    immediately, so we read it straight from the create response and NEVER poll
    ``/config`` (the OOM path). Returns ``info.id`` (an int; the App may return null
    only for dry runs, which never reach here).
    """
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out

    request = out.CreateScanRequestV2(
        scan_metadata=scan_metadata,
        project_metadata=project_metadata,
        project_config=None,
    )
    resp = httpx.post(
        f"{_semgrep_url()}/api/cli/v2/scans",
        headers=_auth_headers(),
        json=request.to_json(),
        timeout=_UPLOAD_TIMEOUT,
    )
    resp.raise_for_status()
    create_response = out.CreateScanResponseV2.from_json(resp.json())
    return create_response.info.id


def _post_results_and_complete(
    scan_id: int,
    ci_scan_results: Any,
    complete: Any,
) -> Any:
    """POST ``/results`` then ``/complete`` via plain authenticated HTTP.

    Replicates ``ScanHandler.report_findings``' two POSTs (same URLs, same JSON
    bodies) but with an explicit ``Authorization: Bearer`` header on each, and
    returns the parsed ``out.CiScanCompleteResponse`` for the block decision.
    """
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out

    base = f"{_semgrep_url()}/api/agent/scans/{scan_id}"
    headers = _auth_headers()

    results_resp = httpx.post(
        f"{base}/results",
        headers=headers,
        json=ci_scan_results.to_json(),
        timeout=_UPLOAD_TIMEOUT,
    )
    results_resp.raise_for_status()

    complete_resp = httpx.post(
        f"{base}/complete",
        headers=headers,
        json=complete.to_json(),
        timeout=_UPLOAD_TIMEOUT,
    )
    complete_resp.raise_for_status()
    return out.CiScanCompleteResponse.from_json(complete_resp.json())


def _report_failure(scan_id: int, exit_code: int) -> None:
    """Close an open scan via ``/error`` so the App never wedges the PR check.

    Same endpoint and body shape as ``ScanHandler.report_failure``
    (``POST /api/agent/scans/{scan_id}/error`` with ``{"exit_code", "stderr"}``),
    plus the explicit Bearer header.
    """
    resp = httpx.post(
        f"{_semgrep_url()}/api/agent/scans/{scan_id}/error",
        headers=_auth_headers(),
        json={"exit_code": exit_code, "stderr": ""},
        timeout=_UPLOAD_TIMEOUT,
    )
    resp.raise_for_status()


async def report_pr_scan(
    *,
    repo: str,
    branch: str,
    commit: str,
    pr_id: str | None = None,
    base_ref: Optional[str] = None,
    raw_cli_output: dict[str, Any] | str,
    project_id: Optional[str] = None,
    repo_url: Optional[str] = None,
    scan_execution_duration: Optional[float] = None,
    cohort: Optional[dict] = None,
    dry_run: bool = False,
    is_full_scan: bool = False,
) -> dict[str, Any]:
    """Report an fc-invoke scan to the Semgrep App (non-blocking async wrapper).

    The actual work (mapping cli_output into ``out.Finding`` objects and the
    synchronous CREATE / ``/results`` / ``/complete`` ``httpx`` POSTs) is CPU- and
    sync-IO-bound and does NOT await, so running it inline on the event loop would
    block it, briefly for a PR diff, longer for a whole-repo full scan (hundreds
    of findings + a ~224 KiB payload). Run it in a worker thread so the API pod's
    event loop keeps serving live requests. The thread never touches loop objects
    (plain env reads + sync httpx), so it is safe.
    """
    return await asyncio.to_thread(
        _report_pr_scan_blocking,
        repo=repo,
        branch=branch,
        commit=commit,
        pr_id=pr_id,
        base_ref=base_ref,
        raw_cli_output=raw_cli_output,
        project_id=project_id,
        repo_url=repo_url,
        scan_execution_duration=scan_execution_duration,
        cohort=cohort,
        dry_run=dry_run,
        is_full_scan=is_full_scan,
    )


def _report_pr_scan_blocking(
    *,
    repo: str,
    branch: str,
    commit: str,
    pr_id: str | None = None,
    base_ref: Optional[str] = None,
    raw_cli_output: dict[str, Any] | str,
    project_id: Optional[str] = None,
    repo_url: Optional[str] = None,
    scan_execution_duration: Optional[float] = None,
    cohort: Optional[dict] = None,
    dry_run: bool = False,
    is_full_scan: bool = False,
) -> dict[str, Any]:
    """Report an fc-invoke scan to the Semgrep App, built from cli_output.

    Opens a scan (CREATE POST, which needs NO rules and returns the scan_id
    immediately), maps ``raw_cli_output`` DIRECTLY into ``out.Finding`` objects (no
    ``RuleMatch``, no rule loading), POSTs them (``/results`` + ``/complete``), and
    returns the App's block decision. Every request carries an explicit
    ``Authorization: Bearer $SEMGREP_APP_TOKEN`` header. ``raw_cli_output`` may be
    the already-parsed cli_output ``dict`` (the real fc-invoke client shape) or the
    raw JSON ``str``. ``scan_execution_duration`` (seconds), when passed, is the
    engine scan time the webhook measured around fc-invoke; it is reported as the
    App scan's ``total_time`` (else the App shows 0 for a self-reported scan).

    ``is_full_scan=False`` (the default) is the per-PR diff scan path: ``pr_id``
    is required and threaded into the App's ``pull_request_id``, exactly as
    before. ``is_full_scan=True`` is the whole-repo interfile FULL scan (the
    ``semgrep-full`` workload, reported ``"on": "schedule"``): ``pr_id`` is not
    needed (pass None, the default) and the App's ``pull_request_id`` is sent as
    an empty string, which the ProjectMetadata schema requires.

    On ANY failure after the scan is opened it POSTs ``/error`` in the exception
    path so the App never wedges the PR check on an open scan. ``SEMGREP_APP_TOKEN``
    is read from the environment; this module never hardcodes it.

    With ``dry_run=True`` NO network happens at all: we assemble and serialize both
    payloads (proving they are well-formed) but skip the CREATE / ``/results`` /
    ``/complete`` POSTs entirely. Used by tests and any caller that wants to
    assemble the payload without touching the live App.
    """
    # The repository the App scan lands under (the shadow project when
    # SEMGREP_SHADOW_PROJECT is set, else the real repo), surfaced so the webhook
    # can build the App scan URL for its commit status. ``org`` is the owner
    # segment of the reported project (jomcgi for jomcgi/homelab-selfhosted); the
    # App scan URL is scoped by org, not by repo.
    reported_project = _reported_repository(repo)
    org = reported_project.split("/", 1)[0] if "/" in reported_project else "jomcgi"

    result: dict[str, Any] = {
        "ok": False,
        "dry_run": dry_run,
        "scan_id": None,
        "findings_reported": 0,
        "app_block_override": False,
        "app_block_reason": "",
        "app_blocking_match_based_ids": [],
        "error": None,
        # The Semgrep project + org the scan was reported under (shadow-aware), so
        # the caller can build the App scan URL without re-reading the env.
        "project": reported_project,
        "org": org,
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
    complete = _build_ci_scan_complete(findings_count, scan_execution_duration)

    project_metadata = _build_project_metadata(
        repo=repo,
        branch=branch,
        commit=commit,
        pr_id=pr_id,
        base_ref=base_ref,
        project_id=project_id,
        repo_url=repo_url,
        is_full_scan=is_full_scan,
    )

    if dry_run:
        # Assemble-only: serialize both payloads so the dry run proves they are
        # well-formed, but do NO network (no CREATE, no /results, no /complete).
        try:
            _build_scan_metadata()
            ci_scan_results.to_json()
            complete.to_json()
        except Exception as exc:  # noqa: BLE001 - surface serialization failure
            logger.exception("dry_run payload assembly failed")
            result["error"] = str(exc) or type(exc).__name__
            return result
        result["ok"] = True
        return result

    scan_id: Optional[int] = None
    try:
        scan_metadata = _build_scan_metadata()
        scan_id = _create_scan(scan_metadata, project_metadata)
        result["scan_id"] = scan_id

        complete_response = _post_results_and_complete(
            scan_id, ci_scan_results, complete
        )
        result["ok"] = True
        result["app_block_override"] = complete_response.app_block_override
        result["app_block_reason"] = complete_response.app_block_reason
        result["app_blocking_match_based_ids"] = [
            mid.value for mid in complete_response.app_blocking_match_based_ids
        ]

        # Persist a Route B perf row (authoritative runtime = scan_execution_duration)
        # for the private scan-perf comparison page. Best-effort: a perf-store
        # failure must never fail an already-reported scan.
        try:
            from semgrep import __VERSION__ as _sg_cli_version
            from sqlmodel import Session
            from core.db import get_engine
            from semgrep_scan.perf_store import ScanPerf, upsert_scan_perf

            # Stamp the completion time at persist (this runs right after the
            # scan reported complete). scan_completed_at must be set: the perf
            # read query orders by it, and route-b rows without it sort NULLS
            # FIRST in Postgres and starve dated SMS rows past the LIMIT.
            _perf_now = datetime.now(timezone.utc)
            _perf_dur = float(scan_execution_duration or 0.0)
            with Session(get_engine()) as _perf_session:
                upsert_scan_perf(
                    _perf_session,
                    ScanPerf(
                        scan_id=scan_id,
                        environment="route-b",
                        raw_environment=SCAN_ENVIRONMENT,
                        is_full_scan=is_full_scan,
                        branch=branch,
                        scan_ref=(f"refs/pull/{pr_id}/merge" if pr_id else branch),
                        commit_sha=commit,
                        total_time=_perf_dur,
                        findings_total=int(findings_count),
                        cli_version=_sg_cli_version,
                        scan_started_at=_perf_now - timedelta(seconds=_perf_dur),
                        scan_completed_at=_perf_now,
                        # Diff cohort (route-b only; a matched pair inherits it by
                        # commit_sha). None for full scans / when unavailable.
                        file_count=(cohort or {}).get("file_count"),
                        changed_lines=(cohort or {}).get("changed_lines"),
                        languages=(cohort or {}).get("languages"),
                    ),
                )
        except Exception:
            logger.exception("semgrep perf: failed to persist route-b scan_perf row")

        return result
    except Exception as exc:  # noqa: BLE001 - structured error, never kill the process
        logger.exception(
            "report_pr_scan failed (scan_id=%s)", scan_id if scan_id else "unopened"
        )
        result["error"] = str(exc) or type(exc).__name__
        if scan_id:
            try:
                # Close the open scan so the App does not leave the PR check
                # spinning. Exit code 2 = internal error, mirroring the CLI.
                _report_failure(scan_id, 2)
            except Exception:  # noqa: BLE001 - best-effort close
                logger.exception("report_failure also failed; scan may be left open")
        return result
