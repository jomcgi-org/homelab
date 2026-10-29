"""Chat domain: Discord bot, explore and cluster agents."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Register chat domain routers with the app."""
    from chat.router import router

    app.include_router(router)
