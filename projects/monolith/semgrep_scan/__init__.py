"""Semgrep scanning and Semgrep-App reporting.

Two responsibilities live here:

- ``client`` + ``mcp``: the ``semgrep_scan`` MCP tool that forwards changed files
  to EmberVM and returns its findings. The service address is injected from Helm
  values; this package never hardcodes it.
- ``report``: the relay that uploads findings to the Semgrep AppSec
  Platform using pysemgrep's own internal client.

This package is deliberately NOT named ``semgrep``: a top-level ``semgrep``
package on ``sys.path`` would shadow the pip ``semgrep`` distribution, making
pysemgrep's internal modules (``semgrep.app.scans`` etc.) that ``report`` imports
unresolvable. Keeping our name distinct lets both coexist.

- ``router``: the GitHub PR webhook (Phase 2) that fires the scan + App relay on a
  real ``pull_request`` event. Registered on the app like every other domain.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Register the GitHub Semgrep scan webhook with the app.

    Import is local so the module (and its pysemgrep-dependent transitive imports)
    is only pulled in when the app actually wires the router, mirroring the other
    domains' registration.
    """
    from semgrep_scan.router import router

    app.include_router(router)
