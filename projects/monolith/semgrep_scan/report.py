"""Relay fc-invoke Semgrep findings to the Semgrep AppSec Platform.

The fc-invoke semgrep guest scans a PR on our own Firecracker VM and returns the
verbatim ``semgrep --json`` cli_output (``raw_cli_output``, see the guest's
``ScanResult.RawCliOutput``). This module turns that cli_output into the same
``RuleMatch`` objects pysemgrep's CLI builds internally and uploads them under a
per-PR scan using pysemgrep's OWN client (``semgrep.app.scans.ScanHandler``), so
the Semgrep App applies policy/triage server-side and posts the native PR check
exactly as a Managed Scan would. The guest itself stays air-gapped from the App;
the monolith owns the whole App conversation.

VERSION COUPLING (read before bumping semgrep):
    This module depends on pysemgrep INTERNAL, undocumented APIs
    (``ScanHandler``, ``core_output``/``rule_match`` internals,
    ``semgrep_output_v1`` types, ``config_resolver.Config``). The semgrep pin is
    ``semgrep==1.168.0`` in ``bazel/requirements/tools.in`` and MUST match the
    fc-invoke Pro engine version so ``match_based_id`` fingerprints line up
    across the guest scan and this relay. Any bump to that pin REQUIRES
    re-verifying the import surface below and the fingerprint-reproduction test
    in ``report_test.py`` before merging.

THE MAPPING (spike result):
    ``report_findings`` uploads ``match.to_app_finding_format(...)``, whose
    ``match_based_id`` (the App's cross-scan dedup/triage key) is derived from
    ``(rule.formula_string with metavar bindings substituted, path, rule_id)``.
    ``formula_string`` comes from the Rule, not the cli_output, so we must load
    the same rules the guest scanned with. We reconstruct the pipeline the CLI
    uses: cli_output ``results[]`` -> ``out.CoreMatch`` -> ``out.CoreOutput`` ->
    ``core_output.core_matches_to_rule_matches(rules, ...)``. To stay independent
    of how the guest prefixed check_ids (the rule-id rewrite depends on the rules
    directory layout), we load rules with bare ids, match each finding's check_id
    to its rule by exact-or-suffix, and ``rule.rename_id`` the rule to the
    finding's check_id so the reconstructed fingerprint is byte-identical to the
    guest's. This reproduces the guest fingerprints exactly (verified in tests).
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from typing import Iterator
from typing import Optional

logger = logging.getLogger("monolith.semgrep.report")

# The rules the guest scans with are baked into the monolith image at this path
# (local rules + the Pro rule packs), mirroring the guest's SEMGREP_SCAN_RULES.
# Overridable via env so the same var name the guest uses drives both.
RULES_DIR = os.environ.get("SEMGREP_SCAN_RULES", "/etc/semgrep/rules")

# The scan_environment string the App tags our scans with. Distinguishes Route B
# (self-hosted) scans from Semgrep Managed Scans in the dashboard.
SCAN_ENVIRONMENT = "homelab-fc-invoke"


_semgrep_home_ready = False


def _ensure_writable_semgrep_home() -> None:
    """Point pysemgrep at a writable HOME + data/log/settings dirs.

    The monolith runs non-root with ``HOME=/`` (unwritable), but several
    pysemgrep code paths resolve writable files under ``Path.home()``:

    * ``semgrep.terminal.Terminal.configure()`` does
      ``env.user_log_file.parent.mkdir(...)``, and ``user_log_file`` defaults to
      ``user_data_folder / "semgrep.log"`` where ``user_data_folder`` is
      ``Path.home() / ".semgrep"`` (unless ``XDG_CONFIG_HOME`` is a valid dir).
      With ``HOME=/`` this tries to ``mkdir /.semgrep`` -> ``PermissionError:
      [Errno 13] Permission denied: '/.semgrep'``.
    * ``Settings`` writes a default settings.yaml (we already redirect that via
      ``SEMGREP_SETTINGS_FILE``).
    * the version-check cache resolves under ``Path.home() / ".cache"``.

    We mirror the guest's ``HOME=/tmp/sghome`` pattern: if ``HOME`` is missing or
    not writable, point it (and the specific semgrep data-dir env vars that drive
    ``user_log_file`` / ``user_data_folder`` in the pinned 1.168.0 ``env.py``) at
    a writable temp dir. Idempotent: safe to call once at every relay entry.

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

    # ``user_data_folder`` = ``$XDG_CONFIG_HOME/.semgrep`` if XDG_CONFIG_HOME is a
    # valid dir, else ``$HOME/.semgrep``. Pin it explicitly so both the data
    # folder and the derived ``user_log_file`` land somewhere writable regardless
    # of any stray XDG_CONFIG_HOME. We point XDG at HOME (now writable).
    semgrep_data = os.path.join(home, ".semgrep")
    os.makedirs(semgrep_data, exist_ok=True)
    os.environ.setdefault("XDG_CONFIG_HOME", home)
    os.environ.setdefault("XDG_CACHE_HOME", home)
    # ``user_log_file`` reads SEMGREP_LOG_FILE first; pin it under the writable
    # data dir so the ``.parent.mkdir`` in Terminal.configure() cannot fail.
    os.environ.setdefault("SEMGREP_LOG_FILE", os.path.join(semgrep_data, "semgrep.log"))

    if not os.environ.get("SEMGREP_SETTINGS_FILE"):
        os.environ["SEMGREP_SETTINGS_FILE"] = os.path.join(
            semgrep_data, "semgrep-relay-settings.yml"
        )

    _semgrep_home_ready = True


@contextlib.contextmanager
def _materialized_sources(files: Optional[list[dict[str, Any]]]) -> Iterator[None]:
    """Materialize the scanned file contents on disk and chdir into them.

    ``RuleMatch.__init__`` EAGERLY reads the matched source lines off disk
    (``rule_match.get_lines`` -> ``util.get_lines_from_file(self.path, ...)``),
    so a finding's ``path`` (repo-relative, e.g. ``repo/newfile.py``) must exist
    on disk when we construct the RuleMatch. The monolith does not have the
    scanned checkout, but it DID send the contents to fc-invoke as
    ``[{path, content}]``; we write those same contents into a temp dir mirroring
    the relative paths and ``os.chdir`` into it so the relative reads resolve. The
    REPORTED path stays repo-relative (correct for the App) because we only change
    cwd, never the finding paths.

    CAUTION: ``os.chdir`` is process-global, so scans MUST NOT run concurrently
    in-process while this is active. That is fine for the v1 sequential webhook
    path; the Phase 2 concurrency/perf work must revisit this (e.g. a subprocess
    or a lines-provider that reads from an in-memory map instead of cwd).

    ``files`` may be ``None``/empty (e.g. a scan with no findings, or a caller
    that has nothing to materialize); in that case this is a no-op chdir-wise but
    still restores cwd, so the mapping simply finds no source to read.
    """
    if not files:
        yield
        return

    original_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="semgrep-relay-src-") as tmpdir:
        for entry in files:
            rel = entry.get("path")
            content = entry.get("content")
            if not rel or content is None:
                continue
            # Join defensively: reject absolute paths / parent escapes so a
            # malicious or malformed path cannot write outside the temp dir.
            dest = (Path(tmpdir) / rel).resolve()
            if not str(dest).startswith(str(Path(tmpdir).resolve())):
                logger.warning("skipping out-of-tree source path %r", rel)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        try:
            os.chdir(tmpdir)
            yield
        finally:
            os.chdir(original_cwd)


def _iter_rule_files(rules_dir: str) -> Iterator[str]:
    """Yield the individual rule files under ``rules_dir`` (recursively).

    If ``rules_dir`` is itself a single file, yield just that. Otherwise walk the
    tree yielding ``.yaml`` / ``.yml`` files. Loading files one at a time is what
    keeps peak memory bounded (see ``_load_fired_rules``).
    """
    if os.path.isfile(rules_dir):
        yield rules_dir
        return
    for root, _dirs, names in os.walk(rules_dir):
        for name in sorted(names):
            if name.endswith((".yaml", ".yml")):
                yield os.path.join(root, name)


def _bare_id_matches_check_id(bare_id: str, check_id: str) -> bool:
    """Whether a loaded rule's bare id resolves a fired check_id.

    Same semantics as ``_find_rule_for_check_id``: exact id, or the bare id is a
    dotted suffix of the (possibly prefixed) check_id.
    """
    return check_id == bare_id or check_id.endswith("." + bare_id)


def _load_fired_rules(rules_dir: str, check_ids: set[str]) -> dict[str, Any]:
    """Load ONLY the rules whose bare id resolves a fired ``check_id``.

    MEMORY: loading the whole rules dir at once (~1000+ rules across local + Pro
    packs) OOM-kills the 1Gi monolith. A PR diff fires only a handful of rules,
    so we load rule FILES one at a time, keep only the rules that resolve a fired
    check_id, discard the rest immediately, and stop as soon as every fired
    check_id is resolved. Peak memory is therefore bounded by one file's rules
    plus the handful of kept rules, never the whole corpus.

    Returns a dict keyed by each kept rule's bare (un-rewritten) id, matching the
    old ``_load_rules_by_bare_id`` shape so ``_find_rule_for_check_id`` is
    unchanged. check_ids that resolve to no rule are simply absent (the caller
    reports them as unmatched, never loading everything as a fallback).
    """
    from semgrep.config_resolver import Config

    if not check_ids:
        return {}

    by_bare: dict[str, Any] = {}
    unresolved = set(check_ids)
    for rule_file in _iter_rule_files(rules_dir):
        if not unresolved:
            break
        try:
            cfg, errors = Config.from_config_list([rule_file], project_url=None)
        except Exception:  # noqa: BLE001 - one bad file must not sink the relay
            logger.warning("failed to load semgrep rule file %s; skipping", rule_file)
            continue
        if errors:
            # Non-fatal: a single bad rule file must not sink the whole relay.
            logger.warning(
                "semgrep rule load reported %d error(s) from %s",
                len(errors),
                rule_file,
            )
        # get_rules materializes this file's rules; we keep only the fired ones
        # and let the rest go out of scope at the end of the loop iteration.
        for rule in cfg.get_rules(no_rewrite_rule_ids=True):
            matched = {
                cid for cid in unresolved if _bare_id_matches_check_id(rule.id, cid)
            }
            if matched:
                by_bare[rule.id] = rule
                unresolved -= matched
    return by_bare


def _find_rule_for_check_id(check_id: str, by_bare: dict[str, Any]) -> Optional[Any]:
    """Match a cli_output check_id to a loaded rule, prefix-independent.

    The guest may emit fully-prefixed check_ids (e.g.
    ``rules.python.flask.os.tainted-os-command-stdlib-flask.<name>``) while our
    loaded rules carry bare ids. Match by exact id, else by the longest bare id
    that is a dotted suffix of the check_id.
    """
    exact = by_bare.get(check_id)
    if exact is not None:
        return exact
    candidates = [
        rule
        for bid, rule in by_bare.items()
        if check_id == bid or check_id.endswith("." + bid)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: len(r.id), reverse=True)
    return candidates[0]


def _cli_result_to_core_match(result: dict[str, Any]) -> Any:
    """Build an ``out.CoreMatch`` from one cli_output ``results[]`` entry.

    The cli_output extra is a superset of core_match_extra; we forward exactly
    the fields ``CoreMatch.from_json`` reads (metavars/engine_kind/is_ignored are
    required, the rest optional). ``fingerprint``/``lines`` are cli-only and
    intentionally dropped: the fingerprint is recomputed and must match.
    """
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out

    extra = result["extra"]
    return out.CoreMatch.from_json(
        {
            "check_id": result["check_id"],
            "path": result["path"],
            "start": result["start"],
            "end": result["end"],
            "extra": {
                "metavars": extra["metavars"],
                "engine_kind": extra["engine_kind"],
                "is_ignored": extra.get("is_ignored", False),
                "message": extra.get("message"),
                "metadata": extra.get("metadata"),
                "severity": extra.get("severity"),
                "dataflow_trace": extra.get("dataflow_trace"),
                "validation_state": extra.get("validation_state"),
            },
        }
    )


def map_cli_output_to_matches(
    raw_cli_output: dict[str, Any] | str,
    rules_dir: str = RULES_DIR,
    files: Optional[list[dict[str, Any]]] = None,
) -> tuple[Any, list[Any], list[dict[str, Any]]]:
    """Map a ``semgrep --json`` cli_output to pysemgrep RuleMatches.

    ``raw_cli_output`` may be EITHER the already-parsed cli_output ``dict`` (the
    real fc-invoke client shape: the client does ``resp.json()`` so the embedded
    JSON object arrives already parsed) OR the raw JSON ``str`` (the test-fixture
    shape). Both are accepted.

    ``files`` is the same ``[{path, content}]`` list the caller sent to
    fc-invoke's ``/invoke/semgrep``. It is REQUIRED whenever there are findings:
    ``RuleMatch.__init__`` eagerly reads the matched source lines off disk, so
    the finding paths must exist on disk during construction. We materialize the
    contents into a temp dir and chdir into it (see ``_materialized_sources``).

    Returns ``(filtered_matches, rules_with_matches, unmatched)`` where
    ``filtered_matches`` is a ``FilteredMatches`` ready for
    ``report_findings``, ``rules_with_matches`` is the ``List[Rule]`` for the
    matched findings (renamed to the findings' check_ids so fingerprints line
    up), and ``unmatched`` is the list of cli results whose check_id resolved to
    no loaded rule (skipped, so a missing rule never crashes the relay).
    """
    _ensure_writable_semgrep_home()

    import semgrep.semgrep_interfaces.semgrep_output_v1 as out
    from semgrep.core_output import core_matches_to_rule_matches
    from semgrep.types import FilteredMatches

    # Bug 1: the fc-invoke client returns the whole response via resp.json(), so
    # raw_cli_output arrives already parsed as a dict. Accept dict OR str.
    cli = (
        raw_cli_output
        if isinstance(raw_cli_output, dict)
        else json.loads(raw_cli_output)
    )
    results = cli.get("results", [])

    # Bug 3 (OOM): load ONLY the rules whose check_id actually fired. Loading the
    # whole rules dir (~1000+ rules) at once OOM-kills the 1Gi monolith; a PR diff
    # fires a handful of rules, so we first collect the fired check_ids and load
    # only those (peak memory stays bounded, see _load_fired_rules).
    fired_check_ids = {result["check_id"] for result in results}
    by_bare = _load_fired_rules(rules_dir, fired_check_ids)

    # rule_table keyed by the finding's check_id -> the (renamed) matching Rule.
    # A rule object is shared across findings of the same check_id; rename once.
    rule_table: dict[str, Any] = {}
    core_matches: list[Any] = []
    unmatched: list[dict[str, Any]] = []
    for result in results:
        check_id = result["check_id"]
        rule = rule_table.get(check_id)
        if rule is None:
            rule = _find_rule_for_check_id(check_id, by_bare)
            if rule is None:
                logger.warning(
                    "no loaded rule matches check_id %s; skipping finding", check_id
                )
                unmatched.append(result)
                continue
            # rename_id mutates the rule's raw id; scope a copy per check_id so
            # renaming for one finding cannot corrupt a different check_id that
            # happened to resolve to the same bare rule.
            rule = copy.deepcopy(rule)
            rule.rename_id(check_id)
            rule_table[check_id] = rule
        core_matches.append(_cli_result_to_core_match(result))

    core_output = out.CoreOutput.from_json(
        {
            "version": cli.get("version", ""),
            "results": [cm.to_json() for cm in core_matches],
            "errors": [],
            "paths": {"scanned": []},
            "skipped_rules": [],
        }
    )

    # core_matches_to_rule_matches constructs RuleMatch objects, which eagerly
    # read the matched source lines off disk (relative to cwd). Materialize the
    # scanned contents and chdir into them so those reads resolve. This does NOT
    # affect match_based_id: the fingerprint is (formula, path, rule_id) and does
    # not depend on the line contents, so it stays byte-identical.
    with _materialized_sources(files):
        by_rule = core_matches_to_rule_matches(
            list(rule_table.values()), core_output, fips_mode=False
        )
    kept = {rule: matches for rule, matches in by_rule.items() if matches}
    filtered = FilteredMatches(kept=kept, removed=defaultdict(list))
    return filtered, list(kept.keys()), unmatched


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
    guard (only report_findings / report_failure gate on dry_run), and on a bad
    token it calls ``sys.exit`` (a BaseException). So dry_run cannot rely on
    calling the real ``start_scan``. Instead we set ``scan_response`` to this
    stub so ``scan_id`` and the engine-param properties report_findings reads
    (``autofix`` / ``dependency_query``) resolve without any network.
    """

    class _Info:
        id = None  # scan id is null for dry runs, matching the real API

    class _EngineParams:
        autofix = False
        dependency_query = False

    info = _Info()
    engine_params = _EngineParams()


async def report_pr_scan(
    *,
    repo: str,
    branch: str,
    commit: str,
    pr_id: str,
    base_ref: Optional[str] = None,
    raw_cli_output: dict[str, Any] | str,
    files: Optional[list[dict[str, Any]]] = None,
    project_id: Optional[str] = None,
    repo_url: Optional[str] = None,
    rules_dir: str = RULES_DIR,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Report an fc-invoke PR scan to the Semgrep App via pysemgrep's client.

    Opens a per-PR scan (``start_scan``), maps ``raw_cli_output`` to RuleMatches,
    uploads them (``report_findings`` -> POST /results + /complete), and returns
    the App's block decision. ``raw_cli_output`` may be the already-parsed
    cli_output ``dict`` (the real fc-invoke client shape) or the raw JSON ``str``
    (see ``map_cli_output_to_matches``). On ANY failure after the scan is opened it calls
    ``report_failure`` in a finally so the App never wedges the PR check on an
    open scan. ``SEMGREP_APP_TOKEN`` is read from the environment by pysemgrep;
    this module never hardcodes it.

    ``files`` is the same ``[{path, content}]`` list sent to fc-invoke; it is
    required to materialize source lines for the RuleMatch construction (see
    ``map_cli_output_to_matches``).

    With ``dry_run=True`` NO network happens at all: we skip the real
    ``start_scan`` (which POSTs with no dry_run guard) by stubbing
    ``scan_response``, and pysemgrep's ``report_findings`` short-circuits its
    uploads on ``dry_run``. Used by tests and any caller that wants to assemble
    the payload without touching the live App.
    """
    _ensure_writable_semgrep_home()

    # Imports are function-local so importing this module (e.g. for the mapping
    # helpers or in the webhook router) does not eagerly pull the heavy pysemgrep
    # app/state machinery unless a scan is actually reported.
    from semgrep.app.project_config import ProjectConfig
    from semgrep.app.scans import ScanHandler
    from semgrep.engine import EngineType
    from semgrep.parsing_data import ParsingData
    from semgrep.types import TargetInfo
    import semgrep.semgrep_interfaces.semgrep_output_v1 as out
    from rich.progress import Progress

    result: dict[str, Any] = {
        "ok": False,
        "dry_run": dry_run,
        "scan_id": None,
        "findings_reported": 0,
        "unmatched_findings": 0,
        "app_block_override": False,
        "app_block_reason": "",
        "app_blocking_match_based_ids": [],
        "error": None,
    }

    try:
        filtered, rules_with_matches, unmatched = map_cli_output_to_matches(
            raw_cli_output, rules_dir=rules_dir, files=files
        )
    except Exception as exc:  # noqa: BLE001 - surface mapping failure as structured
        logger.exception("failed to map cli_output to RuleMatches")
        result["error"] = f"mapping failed: {exc}"
        return result

    findings_count = sum(len(ms) for ms in filtered.kept.values())
    result["findings_reported"] = findings_count
    result["unmatched_findings"] = len(unmatched)

    project_metadata = _build_project_metadata(
        repo=repo,
        branch=branch,
        commit=commit,
        pr_id=pr_id,
        base_ref=base_ref,
        project_id=project_id,
        repo_url=repo_url,
    )

    targets = {
        TargetInfo(fpath=Path(rm.path), original=None)
        for matches in filtered.kept.values()
        for rm in matches
    }

    handler = ScanHandler(enable_transitive_reachability=None, dry_run=dry_run)

    scan_opened = False
    try:
        if dry_run:
            # Do NOT call the real start_scan: it POSTs to semgrep.dev with no
            # dry_run guard and sys.exit()s on a bad token. Stub the response so
            # scan_id + engine-param properties resolve with zero network.
            handler.scan_response = _DryRunScanResponse()  # type: ignore[assignment]
        else:
            handler.start_scan(project_metadata, ProjectConfig())
        scan_opened = True
        result["scan_id"] = handler.scan_id

        complete = handler.report_findings(
            matches_by_rule=filtered,
            rules=rules_with_matches,
            targets=targets,
            skipped_paths=set(),
            renamed_targets=set(),
            ignored_targets=frozenset(),
            # A diff scan with findings exits non-zero in the CLI; the App uses
            # its own policy to decide blocking, this is just the CLI hint.
            cli_suggested_exit_code=1 if findings_count else 0,
            parse_rate=ParsingData(),
            total_time=0.0,
            commit_date=str(int(time.time())),
            lockfile_dependencies={},
            dependency_parser_errors=[],
            all_subprojects=[],
            contributions=out.Contributions([]),
            engine_requested=EngineType.PRO_INTERFILE,
            progress_bar=Progress(),
            disable_nosem=True,
        )
        result["ok"] = True
        result["app_block_override"] = complete.app_block_override
        result["app_block_reason"] = complete.app_block_reason
        result["app_blocking_match_based_ids"] = [
            mid.value for mid in complete.app_blocking_match_based_ids
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
