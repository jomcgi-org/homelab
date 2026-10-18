"""Stars domain: best dark-sky viewing windows for grid-sourced sites.

The site list lives in stars.sites, populated by the stars.load_grid job from a
light-pollution grid (grid.json) uploaded to R2 out-of-band (ADR 006). A
scheduled refresh job fetches MET Norway forecasts for each site, scores each
dark hour, and stores all future qualifying hours in Postgres
(stars.site_hours). An hourly prune drops elapsed hours; the read endpoint
filters defensively and is the source of truth.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from stars.router import router

    app.include_router(router)


def register_public(app: FastAPI) -> None:
    """Stars is a wholly public, read-only domain: reuse register."""
    register(app)
