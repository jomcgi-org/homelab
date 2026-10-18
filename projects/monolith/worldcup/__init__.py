"""World Cup 2026 Scotland qualification tracker (schema 'worldcup')."""


def register(app) -> None:
    """Register the worldcup router with the app.

    The router + models are safe for the public binary (read-only endpoint).
    The import stays inside the function only to keep import side effects
    contained; it does NOT pull jobs/client/sim.
    """
    from worldcup.router import router

    app.include_router(router)


# Read-only summary endpoint, safe to mount in the public binary.
register_public = register
