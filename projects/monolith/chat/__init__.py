"""Chat domain — Discord bot, backfill, and explore agent."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Register chat domain routers with the app."""
    from chat.router import router
    from chat.whatsapp_inbound import router as whatsapp_inbound_router

    app.include_router(router)
    # /internal/whatsapp/inbound: the WhatsApp gateway forwards allow-listed group
    # messages here (ADR 039). Bearer-authenticated; in-cluster only, like the
    # former agent progress sink.
    app.include_router(whatsapp_inbound_router)
