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


def register_public(app: FastAPI) -> None:
    """dr_jobs is a wholly public, read-only domain: reuse register."""
    register(app)


def on_startup_jobs(session: Session) -> None:
    """Register the daily NHS Scotland scrape job."""
    from scheduler.api import register_job

    from dr_jobs.jobs import scrape_nhs_handler

    register_job(
        session,
        name="dr_jobs.scrape_nhs",
        # Hourly: the scrape is cheap (one list call + ~one heavy detail page
        # per matching job, a handful today) and JobTrain tolerates it easily.
        # Hourly gets a new post (and its Discord ping) in front of the partner
        # within the hour instead of up to a day, and dedup means re-seen jobs
        # only bump last_seen_at, so a faster cadence never re-pings.
        interval_secs=3600,
        handler=scrape_nhs_handler,
        # A run fetches a few heavy detail pages but finishes in well under a
        # minute; 10 min frees the lock promptly if a pod dies mid-scrape
        # without ever overlapping the hourly tick.
        ttl_secs=600,
    )
