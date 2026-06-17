"""dr_jobs: in-monolith NHS Scotland vacancy aggregator (/app/dr-jobs).

Scrapes apply.jobs.scot.nhs.uk daily for anaesthetics-consultant posts and
serves them as a live/history listing table. Same shape as the hikes domain:
a scheduled scrape job upserts a Postgres table, an SSR-only read endpoint backs
the page, and the CDN fans the result out.
"""

from fastapi import FastAPI
from sqlmodel import Session


def register(app: FastAPI) -> None:
    """Register the dr_jobs router with the app."""
    from dr_jobs.router import router

    app.include_router(router)


def on_startup_jobs(session: Session) -> None:
    """Register the daily NHS Scotland scrape job."""
    from shared.scheduler import register_job

    from dr_jobs.jobs import scrape_nhs_handler

    register_job(
        session,
        name="dr_jobs.scrape_nhs",
        interval_secs=86400,  # daily; NHS consultant posts turn over slowly
        handler=scrape_nhs_handler,
        ttl_secs=3600,  # generous: each run fetches a heavy detail page per job
    )
