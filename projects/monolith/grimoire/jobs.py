"""Scheduled job handlers for the grimoire ingest pipeline (spec #4.2).

Two daily, idempotent batch jobs wrap the async orchestrators in ``ingest.py``
and ``extract.py``:

  - ``grimoire_load_chunks``: build an S3 client + embedding client, then run
    ``ingest.load_chunks`` over the ``grimoire`` bucket.
  - ``grimoire_extract_entities``: build an OpenRouter client (skipping the run
    with a warning when OPENROUTER_API_KEY is unset, never crashing the
    scheduler) + embedding client, then run ``extract.extract_chunks``.

Both run off-pod as Argo CronWorkflows: ``app/jobs_main.py`` exposes the
``grimoire-load-chunks`` and ``grimoire-extract-entities`` subcommands (via the
shared ``_run_job`` helper), and the ``jobs.cronWorkflows`` registry in
chart/values.yaml schedules them (loader daily; extraction suspended /
manual-only, since it costs OpenRouter money). Each is an ``async def`` keeping
the scheduler Handler contract (``scheduler.api.Handler``: receives a Session,
returns an optional next-run override), so ``_run_job`` opens a Session and
awaits it directly. This module is excluded from the public binary (grimoire is
private-tier only).
"""

from __future__ import annotations

import logging
import os

from sqlmodel import Session

logger = logging.getLogger("monolith.grimoire.jobs")

DEFAULT_BUCKET = "grimoire"
DEFAULT_EXTRACT_LIMIT = 25


def _embedding_client():
    """Build the shared embedding client, reusing knowledge's DI seam.

    Imported lazily so this module never pulls the embedding/knowledge import
    closure at grimoire import time.
    """
    from knowledge.api import get_embedding_client

    return get_embedding_client()


def _extract_limit() -> int:
    """Read GRIMOIRE_EXTRACT_LIMIT, falling back to the default on a bad value."""
    raw = os.environ.get("GRIMOIRE_EXTRACT_LIMIT", str(DEFAULT_EXTRACT_LIMIT))
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "grimoire_extract_entities: invalid GRIMOIRE_EXTRACT_LIMIT %r, using %d",
            raw,
            DEFAULT_EXTRACT_LIMIT,
        )
        return DEFAULT_EXTRACT_LIMIT


async def grimoire_load_chunks(session: Session) -> None:
    """Load S3 chunk manifests into knowledge_chunk + embedding (spec #4.2.1).

    Idempotent: ``load_chunks`` upserts by ``(book_id, chunk_ref)`` and only
    re-embeds new/changed content, so a re-run over unchanged manifests is
    cheap. Returns None so the scheduler advances by the configured interval.
    """
    from grimoire.ingest import build_s3_client, load_chunks

    bucket = os.environ.get("GRIMOIRE_S3_BUCKET", DEFAULT_BUCKET)
    s3_client = build_s3_client()
    embed_client = _embedding_client()
    summary = await load_chunks(session, s3_client, embed_client, bucket)
    logger.info("grimoire_load_chunks done: %s", summary)
    return None


async def grimoire_extract_entities(session: Session) -> None:
    """Extract entities/mentions/relationships from pending chunks (spec #4.2.2).

    Skips the run (logs a warning, does not raise) when OPENROUTER_API_KEY is
    unset so a missing secret degrades this one job rather than crashing the
    scheduler tick. Bounded per run by GRIMOIRE_EXTRACT_LIMIT (default 25) so a
    run fits the job deadline. Returns None so the scheduler advances by the
    configured interval.
    """
    from grimoire.extract import OpenRouterClient, extract_chunks

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        logger.warning(
            "grimoire_extract_entities: OPENROUTER_API_KEY unset, skipping run"
        )
        return None

    limit = _extract_limit()
    or_client = OpenRouterClient(api_key=api_key)
    embed_client = _embedding_client()
    summary = await extract_chunks(session, or_client, embed_client, limit=limit)
    logger.info("grimoire_extract_entities done: %s", summary)
    return None
