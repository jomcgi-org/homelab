"""Grimoire: D&D campaign manager, private-tier monolith domain module.

Follows ADR 010 (privilege-typed module) and ADR 011 (hot-tier schema).
register_public exposes a corpus-global, no-grants read surface for the
public tier (Task 2 of the public-readonly design);
campaign/grant CRUD stays private-tier only.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Register the grimoire router with the app (private tier only)."""
    from grimoire.router import router

    app.include_router(router)


def register_public(app: FastAPI) -> None:
    """Register only the public, read-only Grimoire routes (no campaign/grant
    surface, see grimoire/router_public.py)."""
    from grimoire.router_public import router as public_router

    app.include_router(public_router)
