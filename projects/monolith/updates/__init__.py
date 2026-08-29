"""Private product-update journal domain."""

from __future__ import annotations


def register(app) -> None:
    """Mount the private read API."""
    from updates.router import router

    app.include_router(router)
