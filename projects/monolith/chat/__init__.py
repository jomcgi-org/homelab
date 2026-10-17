"""Chat domain — Discord bot, backfill, and explore agent."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Register chat domain routers with the app."""
    from chat.router import router

    app.include_router(router)
