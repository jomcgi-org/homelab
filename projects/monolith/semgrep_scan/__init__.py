"""Semgrep scanning and Semgrep-App reporting.

Two responsibilities live here:

- ``client`` + ``mcp``: the ``semgrep_scan`` MCP tool that forwards changed files
  to the in-cluster ``fc-invoke`` HTTP daemon and returns its findings. The
  daemon URL is injected from Helm values (``FC_INVOKE_URL``); this package never
  hardcodes the in-cluster service address.
- ``report``: the relay that uploads fc-invoke findings to the Semgrep AppSec
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
    """Register the semgrep webhook router with the app.

    Import is local so the module (and its pysemgrep-dependent transitive imports)
    is only pulled in when the app actually wires the router, mirroring the other
    domains' registration.
    """
    from semgrep_scan.router import internal_router, router

    app.include_router(router)
    app.include_router(internal_router)
