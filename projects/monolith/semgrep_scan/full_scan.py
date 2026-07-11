"""Whole-repo interfile FULL scan: gather all of main, scan, report (ADR agents/044).

This is the third leg of Route B reporting. The PR webhook (``router.py``) scans
only a PR's changed files as a diff; this module gathers EVERY scannable file at
the tip of ``main``, runs them through the ``semgrep-full`` fc-invoke workload
(Semgrep's interfile engine, whole-repo context), and reports the result to the
Semgrep App with ``is_full_scan=True`` on branch ``main``. This is what seeds the
App's baseline for project ``jomcgi/homelab-selfhosted``: without a full scan the
App has nothing to diff a PR scan's findings against.

Reuses ``router.py``'s scannable-extension filter (``_is_scannable``) and GitHub
auth (``_github_headers`` / ``_GITHUB_API``) so both paths agree on what "a file
worth scanning" means and how we talk to the GitHub REST API.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from semgrep_scan.client import scan_files_full
from semgrep_scan.report import report_pr_scan
from semgrep_scan.router import _GITHUB_API
from semgrep_scan.router import _github_headers
from semgrep_scan.router import _is_scannable

logger = logging.getLogger("monolith.semgrep.full_scan")

_GITHUB_TIMEOUT = 30.0

# Path patterns excluded from the whole-repo baseline scan. This mirrors the
# Semgrep App's own "Path ignores" for the SMS project (tests, generated,
# minified), so the self-hosted full scan covers the SAME source set as SMS and
# the two projects compare like-for-like. It also roughly halves the file count
# (1336 -> ~680 here), keeping the interfile scan inside the semgrep-full guest's
# memory and time budget.
_BASELINE_EXCLUDE_SUFFIXES = (
    "_test.py",
    "_test.go",
    ".test.js",
    ".test.ts",
    ".test.tsx",
    ".min.js",
    "_pb2.py",
)
_BASELINE_EXCLUDE_DIRS = ("/testdata/", "/tests/", "/__mocks__/", "/node_modules/")


def _excluded_from_baseline(path: str) -> bool:
    """True if path is a test/generated/minified file the baseline scan skips."""
    if path.endswith(_BASELINE_EXCLUDE_SUFFIXES):
        return True
    slashed = "/" + path
    return any(d in slashed for d in _BASELINE_EXCLUDE_DIRS)


async def _resolve_commit_sha(client: httpx.AsyncClient, repo: str, ref: str) -> str:
    """Resolve ``ref`` (e.g. ``main``) to its current commit sha via the REST API.

    GET /repos/{repo}/commits/{ref} returns the commit object for the ref tip;
    we only need its ``sha``. A 200 with an unexpected body (missing ``sha``)
    raises a clear ``RuntimeError`` rather than an opaque ``KeyError``.
    """
    resp = await client.get(
        f"{_GITHUB_API}/repos/{repo}/commits/{ref}",
        headers=_github_headers(),
    )
    resp.raise_for_status()
    sha = resp.json().get("sha")
    if not sha:
        raise RuntimeError(f"GitHub commits response for {repo}@{ref} had no sha")
    return sha


async def _fetch_file_content(
    client: httpx.AsyncClient, repo: str, path: str, ref: str
) -> str | None:
    """Fetch one file's text at ``ref`` via the contents API, or None on failure.

    Same approach as ``router._fetch_file_content``: GET the base64-encoded
    ``content`` from the contents endpoint and decode it. Returns None (and logs)
    on any failure so one unreadable file never aborts the whole gather.
    """
    try:
        resp = await client.get(
            f"{_GITHUB_API}/repos/{repo}/contents/{path}",
            headers=_github_headers(),
            params={"ref": ref},
        )
        resp.raise_for_status()
        payload = resp.json()
        encoded = payload.get("content")
        if not encoded or payload.get("encoding") != "base64":
            return None
        import base64

        return base64.b64decode(encoded).decode("utf-8", errors="replace")
    except Exception:
        logger.exception("full_scan: failed to fetch content for %s@%s", path, ref)
        return None


async def gather_main_files(repo: str, ref: str = "main") -> list[dict]:
    """Gather every scannable file at the tip of ``ref`` into scan input.

    Resolves ``ref`` to its tree via the git trees API (recursive, one call), then
    fetches the content of every blob whose extension ``_is_scannable`` accepts.
    Returns a list of ``{"path": path, "content": text}`` dicts, the same shape
    ``scan_files`` / ``scan_files_full`` expect.

    Logs the gathered file count (so a rate-limit or fetch-failure truncation is
    visible), and logs a WARNING if GitHub reports the tree itself as
    ``truncated`` (it caps very large trees; our repo is ~750 files so this is
    unlikely, but worth surfacing if it ever happens).
    """
    files: list[dict] = []
    timeout = httpx.Timeout(_GITHUB_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            f"{_GITHUB_API}/repos/{repo}/git/trees/{ref}",
            headers=_github_headers(),
            params={"recursive": "1"},
        )
        resp.raise_for_status()
        tree_response = resp.json()

        if tree_response.get("truncated"):
            logger.warning(
                "full_scan: GitHub tree response for %s@%s was truncated; "
                "some files may be missing from the full scan",
                repo,
                ref,
            )

        paths = [
            entry.get("path", "")
            for entry in tree_response.get("tree", [])
            if entry.get("type") == "blob"
            and _is_scannable(entry.get("path", ""))
            and not _excluded_from_baseline(entry.get("path", ""))
        ]

        for path in paths:
            content = await _fetch_file_content(client, repo, path, ref)
            if content is not None:
                files.append({"path": path, "content": content})

    logger.info(
        "full_scan: gathered %d scannable files for %s@%s", len(files), repo, ref
    )
    return files


async def run_full_scan(repo: str = "jomcgi/homelab") -> dict[str, Any]:
    """Gather all of main, run the semgrep-full workload, report to the App.

    1. Gather every scannable file at the tip of ``main`` (and resolve main's
       current commit sha, needed to report the scan against a real commit).
    2. Scan them via the ``semgrep-full`` fc-invoke workload, timing the engine
       call.
    3. Report the result to the Semgrep App as a whole-repo FULL scan
       (``is_full_scan=True``, ``branch="main"``, no ``pr_id``), seeding the
       App's baseline for the project.

    Returns the ``report_pr_scan`` result dict on success, or ``{"error": ...}``
    on an early failure (no files gathered, or the scan itself failed).
    """
    timeout = httpx.Timeout(_GITHUB_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        commit_sha = await _resolve_commit_sha(client, repo, "main")

    files = await gather_main_files(repo, "main")
    if not files:
        logger.error("full_scan: no scannable files gathered for %s, aborting", repo)
        return {"error": "no files"}

    t_scan = time.monotonic()
    scan = await scan_files_full(files)
    scan_execution_duration = time.monotonic() - t_scan
    if not isinstance(scan, dict) or scan.get("error"):
        error = (scan or {}).get("error") if isinstance(scan, dict) else scan
        logger.error("full_scan: semgrep-full scan failed for %s: %s", repo, error)
        return {"error": error}

    result = await report_pr_scan(
        repo=repo,
        branch="main",
        commit=commit_sha,
        base_ref=None,
        raw_cli_output=scan.get("raw_cli_output"),
        repo_url=f"https://github.com/{repo}",
        scan_execution_duration=scan_execution_duration,
        is_full_scan=True,
    )

    logger.info(
        "run_full_scan: repo=%s files=%d scan_execution_duration=%.2fs "
        "scan_id=%s findings=%s",
        repo,
        len(files),
        scan_execution_duration,
        result.get("scan_id"),
        result.get("findings_reported"),
    )
    return result
