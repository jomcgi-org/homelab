"""MCP tool that runs Semgrep over changed files via EmberVM.

Exposes a single ``semgrep_scan`` tool. It is a thin async wrapper over
``semgrep_scan.client.scan_files``, which POSTs the supplied file contents to
EmberVM and returns the structured findings.
"""

from __future__ import annotations

from core.mcp_app import mcp
from semgrep_scan.client import scan_files


@mcp.tool
async def semgrep_scan(files: list[dict]) -> dict:
    """Scan changed source files for security and correctness issues with Semgrep.

    Send the whole content of each changed file to EmberVM and get back the
    Semgrep findings. Use this to lint a diff before
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
