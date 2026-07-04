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
# Concurrent extract calls; kept below vLLM --max-num-seqs (8) so this bulk job
# leaves decode-slot headroom for trusted interactive callers. See
# grimoire.extract.DEFAULT_CONCURRENCY.
DEFAULT_EXTRACT_CONCURRENCY = 6


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


def _extract_concurrency() -> int:
    """Read GRIMOIRE_EXTRACT_CONCURRENCY (concurrent vLLM extract calls).

    Falls back to the default on a bad or non-positive value; keep it <= the
    vLLM server's --max-num-seqs so requests batch rather than queue.
    """
    raw = os.environ.get(
        "GRIMOIRE_EXTRACT_CONCURRENCY", str(DEFAULT_EXTRACT_CONCURRENCY)
    )
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        logger.warning(
            "grimoire_extract_entities: invalid GRIMOIRE_EXTRACT_CONCURRENCY %r, "
            "using %d",
            raw,
            DEFAULT_EXTRACT_CONCURRENCY,
        )
        return DEFAULT_EXTRACT_CONCURRENCY
    return value


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

    Gates on the configured endpoint, not key presence: the default
    (unset ``GRIMOIRE_EXTRACT_BASE_URL``) or an explicit openrouter.ai URL is
    treated as OpenRouter, which needs OPENROUTER_API_KEY -- skips the run
    (logs a warning, does not raise) if that's unset, so a missing secret
    degrades this one job rather than crashing the scheduler tick. Any other
    base_url (e.g. the in-cluster Qwen vLLM endpoint) runs keyless. Bounded
    per run by GRIMOIRE_EXTRACT_LIMIT (default 25) so a run fits the job
    deadline. Returns None so the scheduler advances by the configured
    interval.
    """
    from grimoire.extract import OpenRouterClient, extract_chunks

    base_url = os.environ.get("GRIMOIRE_EXTRACT_BASE_URL", "")
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    is_openrouter = (not base_url) or "openrouter.ai" in base_url
    if is_openrouter and not api_key:
        logger.warning(
            "grimoire_extract_entities: OpenRouter endpoint but "
            "OPENROUTER_API_KEY unset, skipping run"
        )
        return None

    limit = _extract_limit()
    concurrency = _extract_concurrency()
    or_client = OpenRouterClient(api_key=api_key)  # base_url/model read from env
    embed_client = _embedding_client()
    summary = await extract_chunks(
        session, or_client, embed_client, limit=limit, concurrency=concurrency
    )
    logger.info("grimoire_extract_entities done: %s", summary)
    return None
