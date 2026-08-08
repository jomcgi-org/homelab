"""Agent-built live artifacts.

Agent-built, self-contained HTML pages published to ``s3://artifacts/<id>`` and
served at ``jomcgi.dev/artifact/<id>``. The monolith mediates every write (the
agent guest holds no S3 credential), and the page is framed in a sandboxed
iframe (opaque origin) so untrusted agent markup cannot touch the site origin.

Two surfaces:
- the full monolith gets the write + read routers (``register``): the agent
  publishes here in-cluster.
- the public binary gets the read router only (``register_public``): the SSR
  frontend proxies ``/raw`` + ``/version`` to it. The public tier stays
  read-only (ADR 004), so it never mounts the write router.

Both prefixes are ``/internal/artifact`` and are kept off the public HTTPRoute;
the SSR frontend is the sole public origin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["register", "register_public"]


def register(app: FastAPI) -> None:
    """Register the write + read artifact routers (full monolith)."""
    from artifact.router import read_router, write_router

    app.include_router(write_router)
    app.include_router(read_router)


def register_public(app: FastAPI) -> None:
    """Register only the read artifact router (public, read-only tier)."""
    from artifact.router import read_router

    app.include_router(read_router)
