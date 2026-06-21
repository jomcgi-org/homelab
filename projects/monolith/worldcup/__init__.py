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


def on_startup_jobs(session) -> None:
    """Register the scheduled poll+simulate refresh job.

    Imports are deliberately LAZY (inside the function) so that importing the
    worldcup package does NOT transitively pull jobs/client/sim. The public
    binary imports worldcup but must not drag in the scheduler/httpx/sim graph
    at module load time.
    """
    from scheduler.api import register_job
    from worldcup.jobs import refresh_handler

    register_job(
        session,
        name="worldcup.refresh",
        interval_secs=1800,
        handler=refresh_handler,
        ttl_secs=600,
    )
