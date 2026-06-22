"""Batch-job entrypoint for the monolith (Typer CLI).

This is a second entrypoint baked into the monolith image, distinct from the
API server entrypoint (``app/main.py``). It is built as ``:jobs_image`` from the
same source tree and dependency closure as the backend, so its image layers are
byte-identical to the backend image apart from the launcher: Bazel caches them
and the registry dedupes the blobs, making the jobs image nearly free to build
and push.

Each subcommand runs one batch job to completion and exits. These are the jobs
that previously ran inside the API pod via the in-process scheduler; running
them here keeps the API pod lean and lets the heavy work run in an ephemeral pod
(e.g. an Argo Workflow that invokes ``python app/jobs_main.py <command>`` against
this image).

To add a job: import its handler lazily inside the command function so module
import stays cheap and side-effect free, then dispatch to it.
"""

from __future__ import annotations

import asyncio
import importlib
import logging

import typer

from app.log import configure_logging

logger = logging.getLogger("monolith.jobs")

app = typer.Typer(
    help="Monolith batch jobs. Each command runs one job to completion and exits.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """Run a single monolith batch job to completion and exit.

    The callback is intentionally empty: its presence forces Typer to keep the
    subcommand structure. Without it, a single-command app collapses into a
    bare command, so ``jobs_main.py worldcup-sim`` would reject ``worldcup-sim``
    as an unexpected argument.
    """


@app.command("worldcup-sim")
def worldcup_sim() -> None:
    """Run the World Cup 2026 qualification refresh as a one-shot.

    Polls worldcup26.ir, upserts standings and fixtures, runs the Monte Carlo
    qualification simulation, and persists qualification + swing-match rows.
    This is the one-shot form of the ``worldcup.refresh`` scheduled job; it
    opens its own session to mirror the scheduler's handler contract.
    """
    from sqlmodel import Session

    from app.db import get_engine
    from worldcup.jobs import refresh_handler

    configure_logging()
    logger.info("worldcup-sim: starting")
    with Session(get_engine()) as session:
        asyncio.run(refresh_handler(session))
    logger.info("worldcup-sim: done")


@app.command("knowledge-layout")
def knowledge_layout() -> None:
    """Recompute knowledge-graph node positions as a one-shot.

    Runs the full-graph + public FA2 layout passes (CPU-bound, dispatched to a
    worker thread inside the handler). One-shot form of the ``knowledge.layout``
    scheduled job; opens its own session to mirror the scheduler's handler
    contract. The handler swallows per-pass exceptions and logs them, so a
    silent layout failure still exits 0 - check the ``pass succeeded`` log line.
    """
    from sqlmodel import Session

    from app.db import get_engine
    from knowledge.service import layout_handler

    configure_logging()
    logger.info("knowledge-layout: starting")
    with Session(get_engine()) as session:
        asyncio.run(layout_handler(session))
    logger.info("knowledge-layout: done")


@app.command("observability-topology-rollup")
def observability_topology_rollup() -> None:
    """Build the service-topology payload from ClickHouse and snapshot it to
    Postgres as a one-shot.

    One-shot form of the ``observability.topology_rollup`` scheduled job. Unlike
    the other handlers it takes no session (it opens its own inside the
    snapshot writer), so there is no Session wrapper here. Needs CLICKHOUSE_URL
    plus the CLICKHOUSE_USER / CLICKHOUSE_PASSWORD secret env (cloned into
    monolith-workflows); without them build_topology returns an empty payload.
    """
    from home.observability.rollup import topology_rollup

    configure_logging()
    logger.info("observability-topology-rollup: starting")
    asyncio.run(topology_rollup())
    logger.info("observability-topology-rollup: done")


@app.command("observability-stats-rollup")
def observability_stats_rollup() -> None:
    """Build the cluster-stats payload and snapshot it to Postgres as a one-shot.

    One-shot form of ``observability.stats_rollup``. build_stats counts cluster
    resources via the in-cluster K8s API, so this job runs under a dedicated
    least-privilege SA (monolith-stats) rather than the shared executor SA. Also
    needs CLICKHOUSE_URL + USER/PASSWORD for the GPU metrics queries. No session
    (the snapshot writer opens its own)."""
    from home.observability.rollup import stats_rollup

    configure_logging()
    logger.info("observability-stats-rollup: starting")
    asyncio.run(stats_rollup())
    logger.info("observability-stats-rollup: done")


@app.command("hikes-scrape-walks")
def hikes_scrape_walks() -> None:
    """Run the full WalkHighlands corpus scrape as a one-shot.

    One-shot form of the ``hikes.scrape_walks`` job. The corpus barely changes
    and a full scrape can take a couple of hours, so the CronWorkflow ships
    suspended (manual-only): trigger this by hand when the corpus needs a
    refresh.
    """
    from sqlmodel import Session

    from app.db import get_engine
    from hikes.jobs import scrape_walks_handler

    configure_logging()
    logger.info("hikes-scrape-walks: starting")
    with Session(get_engine()) as session:
        asyncio.run(scrape_walks_handler(session))
    logger.info("hikes-scrape-walks: done")


@app.command("stars-load-climatology")
def stars_load_climatology() -> None:
    """Wholesale-reload the stars climatology table from the S3 backfill.

    One-shot form of the ``stars.load_climatology`` job. The source is a static
    annual CERRA/ERA5 backfill, so the CronWorkflow ships suspended
    (manual-only): trigger this by hand after the backfill is regenerated.
    """
    from sqlmodel import Session

    from app.db import get_engine
    from stars.grid import load_climatology_handler

    configure_logging()
    logger.info("stars-load-climatology: starting")
    with Session(get_engine()) as session:
        asyncio.run(load_climatology_handler(session))
    logger.info("stars-load-climatology: done")


def _run_job(name: str, import_path: str, handler_name: str) -> None:
    """Run a session-taking async scheduler handler as a one-shot.

    Shared body for the simple cutovers: import the handler lazily (keeps CLI
    startup cheap), open a fresh session mirroring the scheduler's contract, and
    run it to completion. Each command below is a thin wrapper so Typer still
    sees a distinct, documented subcommand per job.
    """
    from sqlmodel import Session

    from app.db import get_engine

    handler = getattr(importlib.import_module(import_path), handler_name)
    configure_logging()
    logger.info("%s: starting", name)
    with Session(get_engine()) as session:
        asyncio.run(handler(session))
    logger.info("%s: done", name)


@app.command("ships-heat-rollup")
def ships_heat_rollup() -> None:
    """Rebuild the ships traffic-heat rollup (one-shot of ships.heat_rollup)."""
    _run_job("ships-heat-rollup", "ships.heat", "heat_rollup_handler")


@app.command("ships-partition-maintenance")
def ships_partition_maintenance() -> None:
    """Roll ships position partitions (one-shot of ships.partition_maintenance)."""
    _run_job(
        "ships-partition-maintenance",
        "ships.retention",
        "partition_maintenance_handler",
    )


@app.command("dr-jobs-scrape-nhs")
def dr_jobs_scrape_nhs() -> None:
    """Scrape NHS Scotland vacancies (one-shot of dr_jobs.scrape_nhs)."""
    _run_job("dr-jobs-scrape-nhs", "dr_jobs.jobs", "scrape_nhs_handler")


@app.command("stars-load-grid")
def stars_load_grid() -> None:
    """Reload the stars site grid from S3 (one-shot of stars.load_grid)."""
    _run_job("stars-load-grid", "stars.grid", "load_grid_handler")


@app.command("stars-refresh")
def stars_refresh() -> None:
    """Refresh stars site clear-dark-hours scoring (one-shot of stars.refresh)."""
    _run_job("stars-refresh", "stars.jobs", "refresh_handler")


@app.command("knowledge-repo-docs-reconcile")
def knowledge_repo_docs_reconcile() -> None:
    """Reconcile baked repo-docs manifest into the KG (one-shot of
    knowledge.repo_docs_reconcile)."""
    _run_job(
        "knowledge-repo-docs-reconcile",
        "knowledge.repo_docs",
        "repo_docs_reconcile_handler",
    )


@app.command("hikes-refresh-forecasts")
def hikes_refresh_forecasts() -> None:
    """Refresh hike weather forecasts (one-shot of hikes.refresh_forecasts)."""
    _run_job("hikes-refresh-forecasts", "hikes.jobs", "refresh_forecasts_handler")


@app.command("hikes-prune-windows")
def hikes_prune_windows() -> None:
    """Prune stale hike forecast windows (one-shot of hikes.prune_windows)."""
    _run_job("hikes-prune-windows", "hikes.jobs", "prune_windows_handler")


@app.command("stars-prune-hours")
def stars_prune_hours() -> None:
    """Prune stale stars live-hours rows (one-shot of stars.prune_hours)."""
    _run_job("stars-prune-hours", "stars.jobs", "prune_hours_handler")


@app.command("knowledge-ingest")
def knowledge_ingest() -> None:
    """Drain the knowledge ingest queue (one-shot of knowledge.ingest)."""
    _run_job("knowledge-ingest", "knowledge.ingest_queue", "ingest_handler")


@app.command("knowledge-discover-gaps")
def knowledge_discover_gaps() -> None:
    """Discover knowledge-graph gaps (one-shot of knowledge.discover-gaps)."""
    _run_job("knowledge-discover-gaps", "knowledge.service", "discover_gaps_handler")


@app.command("home-calendar-poll")
def home_calendar_poll() -> None:
    """Fetch the iCal feed and snapshot today's events to Postgres.

    One-shot of home.calendar_poll. The handler takes no session (poll_calendar
    opens its own to write the home.calendar_snapshot row). Needs ICAL_FEED_URL
    from the cloned monolith-secrets secret; without it the poll logs a warning
    and no-ops."""
    from home.schedule import calendar_poll_handler

    configure_logging()
    logger.info("home-calendar-poll: starting")
    asyncio.run(calendar_poll_handler())
    logger.info("home-calendar-poll: done")


if __name__ == "__main__":
    app()
