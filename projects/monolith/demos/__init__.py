"""Firecracker demos domain: authenticated showcase for the fc-invoke workloads.

A thin private-tier API that wraps the existing firecracker-backed handlers
(the sandbox run_code broker, the semgrep scan broker, and the goosecracker
agent run ledger) and the SigNoz trace reader, shaping each response for the
demos page. Every invocation is wrapped in an explicit span so the captured
32-hex ``trace_id`` can be handed to the frontend, which then polls
``/trace/{trace_id}`` to draw the trace waterfall.

This domain is private-tier only: there is no ``register_public`` hook and the
package is never globbed into the public binary (see the monolith BUILD). It is
mounted solely on ``app.main`` (the authenticated entrypoint).
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from demos.firecracker_api import router

    app.include_router(router)
