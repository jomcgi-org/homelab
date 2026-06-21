"""World Cup 2026 Scotland qualification tracker (schema 'worldcup')."""


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
