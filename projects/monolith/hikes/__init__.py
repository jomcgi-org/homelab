"""Hikes: in-monolith Scottish walk planner (migrated from the standalone hikes app)."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Register hikes routers with the app."""
    from hikes.router import router

    app.include_router(router)


def register_public(app: FastAPI) -> None:
    """Hikes is a wholly public, read-only domain: reuse register."""
    register(app)
