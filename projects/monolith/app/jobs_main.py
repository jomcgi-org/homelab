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
import os

import typer

from app.log import configure_logging

logger = logging.getLogger("monolith.jobs")

app = typer.Typer(
    help="Monolith batch jobs. Each command runs one job to completion and exits.",
    no_args_is_help=True,
    add_completion=False,
)


def _run_ember_synthetic() -> None:
    from ember_public.synthetic_probe import (
        probe_bazel,
        probe_pages,
        probe_postgres,
        probe_semgrep,
        record,
    )

    configure_logging()
    logger.info("ember-synthetic: starting")

    async def run() -> None:
        probes = {
            "bazel": probe_bazel(),
            "semgrep": probe_semgrep(),
            "pages": probe_pages(),
            "postgres": probe_postgres(),
        }
        results = await asyncio.gather(*probes.values())
        for demo, result in zip(probes, results):
            if not result["ok"]:
                logger.warning("ember synthetic %s failed: %s", demo, result["detail"])
            await record(demo, result)

    asyncio.run(run())
    logger.info("ember-synthetic: done")


@app.command("ember-synthetic")
def ember_synthetic() -> None:
    """Probe Bazel, Semgrep, pages, and Postgres, recording detector state."""
    # Probe failures intentionally exit 0: health is the failure signal, while
    # Argo retries and failed-job alerts are reserved for DB recording errors.
    _run_ember_synthetic()


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


@app.command("home-cluster-snapshot-refresh")
def home_cluster_snapshot_refresh() -> None:
    """Snapshot the cluster health rollup + firing alerts as a one-shot.

    One-shot form of the retired in-process refresh. refresh_cluster_snapshot
    scans deployments/statefulsets/daemonsets/pods/ArgoCD-apps via the in-cluster
    K8s API, so this job runs under the dedicated least-privilege monolith-stats
    SA (same as observability-stats-rollup), and fetches firing SigNoz alerts
    (SIGNOZ_URL + SIGNOZ_API_KEY). It upserts one home.cluster_snapshot row so the
    dashboard read path is a single-row lookup instead of a live per-request scan.
    Fail-soft per section; the writer opens its own session."""
    from home.cluster_snapshot import refresh_cluster_snapshot

    configure_logging()
    logger.info("home-cluster-snapshot-refresh: starting")
    asyncio.run(refresh_cluster_snapshot())
    logger.info("home-cluster-snapshot-refresh: done")


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


@app.command("campsites-refresh")
def campsites_refresh() -> None:
    """Refresh BC Parks availability and Open-Meteo forecast for /app/campsites."""
    _run_job("campsites-refresh", "campsites.jobs", "refresh_handler")


@app.command("grimoire-load-chunks")
def grimoire_load_chunks() -> None:
    """Load S3 chunk manifests into knowledge_chunk + embedding (spec #4.2.1).

    One-shot of the grimoire chunk loader. Needs the SeaweedFS S3 env (endpoint +
    creds), GRIMOIRE_S3_BUCKET, and EMBEDDING_URL; all are injected into the
    workflows pod by the cronWorkflows `grimoire: true` flag."""
    _run_job("grimoire-load-chunks", "grimoire.jobs", "grimoire_load_chunks")


@app.command("grimoire-extract-entities")
def grimoire_extract_entities() -> None:
    """Extract entities/mentions/relationships from pending chunks (spec #4.2.2).

    One-shot of the grimoire extraction pass. Costs OpenRouter money, so its
    CronWorkflow ships suspended (manual-only). Skips cleanly if
    OPENROUTER_API_KEY is unset; also reads GRIMOIRE_EXTRACT_MODEL /
    GRIMOIRE_EXTRACT_LIMIT and EMBEDDING_URL from the workflows pod env."""
    _run_job("grimoire-extract-entities", "grimoire.jobs", "grimoire_extract_entities")


@app.command("grimoire-backfill-hierarchy")
def grimoire_backfill_hierarchy() -> None:
    """Backfill section_hierarchy onto already-loaded grimoire chunks.

    One-shot of the metadata-only hierarchy backfill: re-runs marker chunking
    over each book's archived raw output.json and writes ONLY the
    section_hierarchy column of matching (book_id, chunk_ref) rows. Never inserts
    a chunk and never re-embeds. Needs the same env as grimoire-load-chunks (S3
    env + GRIMOIRE_S3_BUCKET, injected by the cronWorkflows `grimoire: true`
    flag). Set GRIMOIRE_BACKFILL_BOOK to scope to a single book for a safe verify
    run; unset processes every book with loaded chunks."""
    _run_job(
        "grimoire-backfill-hierarchy", "grimoire.jobs", "grimoire_backfill_hierarchy"
    )


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


@app.command("chat-summary-generation")
def chat_summary_generation() -> None:
    """Generate per-user and per-channel chat summaries (one-shot of
    chat.summary_generation). Needs LLAMA_CPP_URL (Qwen); no bot required."""
    _run_job("chat-summary-generation", "chat.api", "run_summary_generation")


@app.command("chat-changelog")
def chat_changelog(name: str) -> None:
    """Compute the changelog for one config and enqueue it to the Discord outbox
    (one-shot of chat.changelog.<name>; the leader's drain posts it). Needs
    GITHUB_TOKEN, CHANGELOG_CONFIGS, LLAMA_CPP_URL; no bot required."""
    from chat.api import run_changelog_for_config

    configure_logging()
    logger.info("chat-changelog[%s]: starting", name)
    asyncio.run(run_changelog_for_config(name))
    logger.info("chat-changelog[%s]: done", name)


@app.command("chat-drain-reminders")
def chat_drain_reminders() -> None:
    """Deliver due reminders into the Discord outbox (one-shot of
    chat.jobs.drain_reminders_handler). No bot required; the leader's outbox
    drain posts the enqueued rows."""
    _run_job("chat-drain-reminders", "chat.jobs", "drain_reminders_handler")


@app.command("chat-observe-directives")
def chat_observe_directives() -> None:
    """Propose channel directive updates from repeated style corrections.

    One-shot of chat.observer_job.observe_directives_handler. Observes only
    ambient-granted channels (ADR 029), classifies recent bot-directed messages
    for recurring style friction via Qwen (needs LLAMA_CPP_URL), and enqueues at
    most one directive-proposal outbox row per channel; the leader's outbox drain
    posts it and wires the propose-then-confirm flow. Sensitivity is env config:
    OBSERVER_MIN_EVIDENCE (default 3), OBSERVER_COOLDOWN_DAYS (default 14)."""
    _run_job(
        "chat-observe-directives",
        "chat.observer_job",
        "observe_directives_handler",
    )


@app.command("chat-directive-autopilot")
def chat_directive_autopilot() -> None:
    """Silently auto-tune channel and personal behavioural directives from ambient
    interaction signals, self-validate against downstream reactions, and revert on
    regression (one-shot of chat.autopilot_job.directive_autopilot_handler).

    NEVER posts to Discord; provenance is exposed via the monolith-chat-* MCP
    tools. Classifies with Qwen (needs LLAMA_CPP_URL). All thresholds come from
    the AUTOPILOT_* env: AUTOPILOT_MODE (live or shadow kill switch),
    AUTOPILOT_MIN_CONFIDENCE, AUTOPILOT_MIN_EVIDENCE, AUTOPILOT_COOLDOWN_DAYS,
    AUTOPILOT_VALIDATE_DAYS, AUTOPILOT_MANUAL_COOLDOWN_DAYS,
    AUTOPILOT_REGRESS_MARGIN, AUTOPILOT_LOOKBACK_DAYS, AUTOPILOT_MAX_LEN_DELTA."""
    _run_job(
        "chat-directive-autopilot",
        "chat.autopilot_job",
        "directive_autopilot_handler",
    )


@app.command("safeguards-train")
def safeguards_train() -> None:
    """Train the Bosun trust-safeguards random forest (one-shot of
    chat.safeguards_train_job.safeguards_train_handler).

    Gathers the labeled moderation-event dataset, fits the forest inside the
    Firecracker sandbox (needs FC_INVOKE_URL; falls back to an in-process
    numpy fit when the sandbox is unreachable), and stores the result as a
    status='shadow' chat.trust_model row. Skips silently when the dataset is
    below SAFEGUARDS_TRAIN_MIN_SAMPLES / _MIN_POSITIVE / _MIN_NEGATIVE.
    Promotion to status='live' is a manual decision, never this job's."""
    _run_job(
        "safeguards-train",
        "chat.safeguards_train_job",
        "safeguards_train_handler",
    )


@app.command("whatsapp-morning-digest")
def whatsapp_morning_digest() -> None:
    """Send the morning digest to each enabled WhatsApp household group.

    One-shot of whatsapp.morning_digest (ADR 039 spec 5d). Run hourly; it renders
    today's calendar + open reminders into one whatsapp_outbox message per group,
    honouring the group's quiet hours and deduping to one digest per local day.
    DB-only (reads groups/reminders/drafts + the home calendar snapshot, enqueues
    outbox rows); no bot or external creds needed. WHATSAPP_TZ sets the local tz
    (default America/Vancouver)."""
    _run_job(
        "whatsapp-morning-digest", "chat.whatsapp_digest", "morning_digest_handler"
    )


@app.command("evict-artifact-sessions")
def evict_artifact_sessions() -> None:
    """Evict goose session DBs older than the TTL (ADR 026 Phase 2 Task 2.5).

    Sweeps ``s3://artifacts/<id>/sessions.db`` and deletes entries past the TTL so
    abandoned threads' sessions do not accumulate. Artifacts are left intact.
    S3-only, so no DB session is opened. Needs the SeaweedFS S3 env."""
    from artifact.jobs import evict_stale_sessions_handler

    configure_logging()
    logger.info("evict-artifact-sessions: starting")
    evict_stale_sessions_handler()
    logger.info("evict-artifact-sessions: done")


@app.command("semgrep-full-scan-trigger")
def semgrep_full_scan_trigger() -> None:
    """Trigger the whole-repo Semgrep interfile baseline scan of main.

    Deliberately lightweight: it POSTs the running API pod's internal endpoint,
    which fires run_full_scan IN THAT process (where the semgrep package, the
    Semgrep App + GitHub tokens, and a daemon-allowed ServiceAccount already
    live). The heavy scan runs in the API pod, not this ephemeral job pod, so the
    job needs no semgrep deps, no tokens, and no daemon access, just HTTP.
    """
    import httpx

    configure_logging()
    # Injected from Helm (the cron job's env), never hardcoded: a release rename
    # changes the service DNS name, so a baked default would silently break.
    url = os.environ.get("MONOLITH_INTERNAL_URL", "")
    if not url:
        raise RuntimeError("MONOLITH_INTERNAL_URL is not set")
    logger.info("semgrep-full-scan-trigger: POST %s/internal/semgrep/full-scan", url)
    resp = httpx.post(f"{url}/internal/semgrep/full-scan", timeout=30)
    resp.raise_for_status()
    body = resp.json()
    logger.info("semgrep-full-scan-trigger: %s", body)


@app.command("semgrep-harvest-trigger")
def semgrep_harvest_trigger() -> None:
    """Trigger the Semgrep Managed Scans (SMS) scan-perf harvest.

    Deliberately lightweight: it POSTs the running API pod's internal endpoint,
    which fires harvest_scans IN THAT process (where the Semgrep App token and
    a DB session already live). The harvest runs in the API pod, not this
    ephemeral job pod, so the job needs no tokens or DB access, just HTTP.
    """
    import httpx

    configure_logging()
    # Injected from Helm (the cron job's env), never hardcoded: a release rename
    # changes the service DNS name, so a baked default would silently break.
    url = os.environ.get("MONOLITH_INTERNAL_URL", "")
    if not url:
        raise RuntimeError("MONOLITH_INTERNAL_URL is not set")
    logger.info("semgrep-harvest-trigger: POST %s/internal/semgrep/harvest-scans", url)
    resp = httpx.post(f"{url}/internal/semgrep/harvest-scans", timeout=30)
    resp.raise_for_status()
    body = resp.json()
    logger.info("semgrep-harvest-trigger: %s", body)


if __name__ == "__main__":
    app()
