"""Ships: in-monolith AIS vessel tracking (migrated from the standalone marine app)."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Register ships routers with the app."""
    from ships.router import router

    app.include_router(router)


def register_public(app: FastAPI) -> None:
    """Ships is a wholly public, read-only domain: reuse register."""
    register(app)
