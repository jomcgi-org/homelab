"""Chat domain — Discord bot, backfill, and explore agent."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Register chat domain routers with the app."""
    from chat.router import internal_router, router
    from chat.whatsapp_inbound import router as whatsapp_inbound_router

    app.include_router(router)
    # /internal/goosecracker/progress: the build-progress sink for live Discord
    # streaming (ADR 024). Full monolith only (the public tier never runs the bot
    # and must not expose it).
    app.include_router(internal_router)
    # /internal/whatsapp/inbound: the WhatsApp gateway forwards allow-listed group
    # messages here (ADR 039). Bearer-authenticated; in-cluster only, like the
    # goosecracker sink.
    app.include_router(whatsapp_inbound_router)
