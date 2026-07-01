"""Health domain: the worked example of the register(app) convention.

A domain package exposes register(app), which includes its APIRouter on the app.
"""

from __future__ import annotations

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Mount this domain's router on the app."""
    from health.router import router

    app.include_router(router)
