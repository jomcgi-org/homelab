"""Scheduled job handler for the dr_jobs domain.

scrape_nhs_handler runs the daily NHS Scotland vacancy scrape. The network
phase runs in the async handler and the synchronous Session upsert is delegated
to a worker thread (asyncio.to_thread) so it never blocks the scheduler event
loop, mirroring hikes.scrape_walks_handler. The scheduler passes a session, but
the DB work uses its own session inside the thread.

When the upsert inserts genuinely new vacancies (and the run was not the initial
seed of an empty table), the handler posts a one-message Discord digest to the
dr-jobs channel so the partner is pinged the day a post appears.
"""

import asyncio
import logging
import os
from datetime import date, datetime, timezone

import httpx

from dr_jobs import models
from dr_jobs.scraper import fetch_vacancies

logger = logging.getLogger("dr_jobs")

# Client-level ceiling; per-request timeouts in scraper.py are tighter.
SCRAPE_TIMEOUT_SECS = 60.0

# Discord channel for the new-jobs digest (same server as the monolith bot).
# notify() enqueues to the outbox; the leader's bot posts it. The bot must be a
# member of this channel's server (the operative boundary; no app allow-list).
DISCORD_CHANNEL_ID = os.environ.get("DR_JOBS_DISCORD_CHANNEL_ID", "1516663194699960382")

# Cap the digest so a large batch (or first real scrape after a long gap) cannot
# blow past Discord's 2000-char message limit.
_DIGEST_MAX_LISTED = 12

_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# Vacancy dict keys that map straight onto Vacancy columns (timestamps excluded:
# the upsert manages first_seen_at / last_seen_at / scraped_at itself).
_FIELDS = (
    "reference",
    "title",
    "employment_type",
    "salary_band",
    "salary_text",
    "town",
    "postcode",
    "region",
    "posted_date",
    "closing_date",
    "url",
)


def _persist(vacancies: list[dict]) -> tuple[list[dict], int, bool]:
    """Upsert scraped vacancies in a fresh session.

    Returns (new_vacancies, updated_count, was_seed). new_vacancies is the list
    of dicts that were inserted (used for the Discord digest); was_seed is True
    when the table was empty before this run, so the caller suppresses the
    digest on the initial backfill.

    Option A lifecycle: insert unseen JobIds (stamping first_seen_at), refresh
    every field plus last_seen_at on seen ones, and never delete. A vacancy that
    has dropped off the site simply stops having last_seen_at advanced, so the
    read endpoint ages it out of the live view. Runs off the event loop via
    asyncio.to_thread.
    """
    from sqlmodel import Session, func, select

    from app.db import get_engine

    now = datetime.now(timezone.utc)
    new_vacancies: list[dict] = []
    new_rows: list[models.Vacancy] = []
    updated = 0
    with Session(get_engine()) as session:
        pre_count = session.exec(select(func.count()).select_from(models.Vacancy)).one()
        was_seed = pre_count == 0
        for vac in vacancies:
            existing = session.get(models.Vacancy, vac["job_id"])
            if existing is None:
                new_vacancies.append(vac)
                new_rows.append(
                    models.Vacancy(
                        **vac,
                        first_seen_at=now,
                        last_seen_at=now,
                        scraped_at=now,
                    )
                )
                continue
            for field in _FIELDS:
                setattr(existing, field, vac[field])
            existing.last_seen_at = now
            existing.scraped_at = now
            updated += 1
            # session.get rows are tracked; attribute writes flush on commit
            # without a session.add in the loop.

        if new_rows:
            session.add_all(new_rows)
        session.commit()
    return new_vacancies, updated, was_seed


def _fmt_closing(value: date | None) -> str:
    """'12 Jul' for a date, or 'no closing date'."""
    if value is None:
        return "no closing date"
    return f"{value.day} {_MONTHS[value.month - 1]}"


def build_digest(new_vacancies: list[dict]) -> str:
    """Format the Discord digest for newly-seen vacancies (no em-dashes)."""
    n = len(new_vacancies)
    plural = "s" if n != 1 else ""
    lines = [f"\U0001fa7a {n} new NHS Scotland anaesthetics consultant job{plural}:"]
    for vac in new_vacancies[:_DIGEST_MAX_LISTED]:
        where = vac.get("town") or "location TBC"
        lines.append(
            f"• {vac['title'].strip()} · {where}, "
            f"closes {_fmt_closing(vac.get('closing_date'))}"
        )
    if n > _DIGEST_MAX_LISTED:
        lines.append(f"...and {n - _DIGEST_MAX_LISTED} more")
    lines.append("https://jomcgi.dev/app/dr-jobs")
    return "\n".join(lines)


async def _send_digest(new_vacancies: list[dict]) -> None:
    """Post the new-jobs digest to Discord. Never raises (a notify failure must
    not fail an otherwise-successful scrape)."""
    try:
        from agent.api import notify

        await notify(build_digest(new_vacancies), channel=DISCORD_CHANNEL_ID)
    except Exception:  # noqa: BLE001 - best-effort side channel
        logger.warning("dr_jobs: failed to post new-jobs Discord digest", exc_info=True)


async def scrape_nhs_handler(session) -> datetime | None:
    """Daily NHS Scotland scrape: full network phase, then one upsert pass.

    If the scrape produced nothing (transient site outage), keep the existing
    rows and write nothing, so the live view degrades to "stale" rather than
    "empty".
    """
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=SCRAPE_TIMEOUT_SECS
    ) as client:
        vacancies, stats = await fetch_vacancies(client)

    if not vacancies:
        logger.error(
            "dr_jobs scrape: zero vacancies, keeping existing rows (stats: %s)",
            stats,
        )
        return None

    new_vacancies, updated, was_seed = await asyncio.to_thread(_persist, vacancies)
    logger.info(
        "dr_jobs scrape: upserted %d vacancies (%d new, %d updated) seed=%s stats=%s",
        len(vacancies),
        len(new_vacancies),
        updated,
        was_seed,
        stats,
    )

    # Suppress the digest on the initial backfill (every row is "new" then).
    if new_vacancies and not was_seed:
        await _send_digest(new_vacancies)

    return None
