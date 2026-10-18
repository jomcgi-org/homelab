"""dr_jobs: in-monolith NHS Scotland vacancy aggregator (/app/dr-jobs).

Scrapes apply.jobs.scot.nhs.uk hourly for anaesthetics-consultant posts and
serves them as a live/history listing table. Same shape as the hikes domain:
a scheduled scrape job upserts a Postgres table, an SSR-only read endpoint backs
the page, and the CDN fans the result out.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Register the dr_jobs router with the app."""
    from dr_jobs.router import router

    app.include_router(router)


def register_public(app: FastAPI) -> None:
    """dr_jobs is a wholly public, read-only domain: reuse register."""
    register(app)
