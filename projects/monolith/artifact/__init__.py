"""Read existing agent-built artifacts.

Agent-built, self-contained HTML pages published to ``s3://artifacts/<id>`` and
served at ``jomcgi.dev/artifact/<id>``. The page is framed in a sandboxed
iframe (opaque origin) so untrusted agent markup cannot touch the site origin.

Both the full monolith (``register``) and public binary (``register_public``)
mount the read router. The SSR frontend proxies ``/raw`` and ``/version`` to it.

The prefix is ``/internal/artifact`` and is kept off the public HTTPRoute;
the SSR frontend is the sole public origin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["register", "register_public"]


def register(app: FastAPI) -> None:
    """Register the read artifact router (full monolith)."""
    from artifact.router import read_router

    app.include_router(read_router)


def register_public(app: FastAPI) -> None:
    """Register only the read artifact router (public, read-only tier)."""
    from artifact.router import read_router

    app.include_router(read_router)
