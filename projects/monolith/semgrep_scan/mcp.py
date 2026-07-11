"""MCP tool that runs Semgrep over changed files via the fc-invoke daemon.

Exposes a single ``semgrep_scan`` tool. It is a thin async wrapper over
``semgrep_scan.client.scan_files``, which POSTs the supplied file contents to the
in-cluster ``fc-invoke`` HTTP service and returns the structured findings. The
daemon URL is injected from Helm values as ``FC_INVOKE_URL`` and is never
hardcoded here.
"""

from __future__ import annotations

import asyncio
import logging

from app.mcp_app import mcp
from semgrep_scan.client import scan_files, scan_files_full
from semgrep_scan.full_scan import run_full_scan


logger = logging.getLogger("monolith.semgrep.mcp")

# Hold references to background seed tasks so the event loop does not GC them
# mid-run (asyncio only keeps weak refs to tasks).
_seed_tasks: set = set()


@mcp.tool
async def semgrep_full_scan(files: list[dict]) -> dict:
    """Scan a set of files together with whole-program interfile analysis.

    Unlike semgrep_scan (single-file, fast), this routes to the semgrep-full
    fc-invoke workload, which runs the Pro engine over all the supplied files as
    one tree, so cross-file dataflow (taint that flows from one file into
    another) is traced. It is slower and heavier, so use it to check cross-file
    findings, not for a quick single-file lint.

    Args:
        files: A list of file objects, each with a path (repo-relative) and
            content (whole file text). Pass every file that participates in the
            cross-file flow, not just the entrypoint.

    Returns:
        The daemon response (same shape as semgrep_scan), or a dict with a single
        error key on failure.
    """
    return await scan_files_full(files)


@mcp.tool
async def semgrep_seed_baseline(repo: str = "jomcgi/homelab") -> dict:
    """Run a whole-repo interfile full scan of main and report it to the App.

    Gathers every scannable file at the repo main branch, scans them together on
    the semgrep-full workload (interfile), and reports the result to the Semgrep
    App as a full scan on branch main. This seeds the App baseline (primary
    branch plus findings tab) that PR diff scans compare against. It is intended
    for a one-off seed and for the scheduled baseline job, and is heavy (whole
    repo), so it should not be run casually.

    Args:
        repo: The owner/name repo to scan. Defaults to jomcgi/homelab.

    Returns:
        A status dict confirming the background scan was launched. The scan runs
        for up to several minutes, so it is fired as a background task rather
        than awaited. Watch the Semgrep App and the monolith logs for the result.
    """
    task = asyncio.create_task(run_full_scan(repo))
    _seed_tasks.add(task)
    task.add_done_callback(_seed_tasks.discard)
    logger.info("semgrep_seed_baseline: launched background full scan for %s", repo)
    return {
        "status": "started",
        "repo": repo,
        "note": "runs in the background, see the Semgrep App and monolith logs",
    }


@mcp.tool
async def semgrep_scan(files: list[dict]) -> dict:
    """Scan changed source files for security and correctness issues with Semgrep.

    Send the whole content of each changed file to the in-cluster fc-invoke
    daemon and get back the Semgrep findings. Use this to lint a diff before
    committing or to triage a file you just edited.

    Args:
        files: A list of file objects. Each object needs a ``path`` (the repo
            relative file path, used to pick rules and report locations) and a
            ``content`` (the entire current text of that file). Pass the whole
            file, not just the changed lines, so Semgrep has full context.

    Returns:
        On success, the daemon response: a ``findings`` list (each finding has
        ``path``, ``line``, ``col``, ``rule_id``, ``severity``, ``message``) plus
        an ``errors`` list for any per-file scan problems. On failure, a dict
        with a single ``error`` key describing what went wrong.
    """
    return await scan_files(files)
