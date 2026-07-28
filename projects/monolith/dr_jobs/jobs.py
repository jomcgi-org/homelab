"""Scheduled job handler for the dr_jobs domain.

scrape_nhs_handler runs the daily NHS Scotland vacancy scrape. The network
phase runs in the async handler and the synchronous Session upsert is delegated
to a worker thread (asyncio.to_thread) so it never blocks the scheduler event
loop, mirroring hikes.scrape_walks_handler. The scheduler passes a session, but
the DB work uses its own session inside the thread.

Notification state lives on the vacancy row (``notified_discord`` /
``notified_whatsapp``), so "was this posting announced?" is a column check, not
cross-system archaeology. After the upsert the handler claims un-notified,
still-open rows, enqueues one digest per channel directly to the outbox, and
stamps the column only on a successful enqueue, so a failed send is retried next
run (self-healing). Rows inserted on the initial seed of an empty table are born
already-stamped, so the switchover never dumps the existing backlog.
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

# Discord channel for the new-jobs digest (guild/server 1501965852042330302,
# channel #anna-jobs 1516663194699960382). We enqueue straight to
# chat.discord_outbox; the leader's bot drains and posts it. The bot being a
# member of the channel's server is the operative boundary (no app allow-list).
# Enqueuing directly (not via agent.notify) is what keeps the DATABASE_URL-only
# Argo job pod able to notify at all.
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

    from core.db import get_engine

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
            if was_seed:
                # Backlog on an empty-table deploy is born already-notified so
                # the first scrape does not dump every existing posting into the
                # chat. Symmetric with the migration's backfill of prod rows.
                for row in new_rows:
                    row.notified_discord = now
                    row.notified_whatsapp = now
            session.add_all(new_rows)
        session.commit()
    return new_vacancies, updated, was_seed


def _pending(session, column, today: date) -> list[models.Vacancy]:
    """Vacancies not yet notified on ``column`` and not past their closing date.

    An open closing date (or none) keeps a posting eligible; a closed one is
    skipped so a retry cannot resurrect a stale listing.
    """
    from sqlmodel import or_, select

    return list(
        session.exec(
            select(models.Vacancy).where(
                column.is_(None),
                or_(
                    models.Vacancy.closing_date.is_(None),
                    models.Vacancy.closing_date >= today,
                ),
            )
        ).all()
    )


def _digest_dicts(rows: list[models.Vacancy]) -> list[dict]:
    """Minimal dicts (title/town/closing_date) that build_digest consumes."""
    return [
        {"title": r.title, "town": r.town, "closing_date": r.closing_date} for r in rows
    ]


def _stamp(rows: list[models.Vacancy], attr: str, when: datetime) -> None:
    """Mark ``rows`` notified on ``attr``; they are session-tracked and flush on
    the caller's commit."""
    for row in rows:
        setattr(row, attr, when)


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


def _notify_pending_sync() -> None:
    """Claim un-notified, still-open vacancies and deliver one digest per channel.

    Source of truth is the row: ``notified_<channel> IS NULL`` plus an open
    closing date means pending. On a successful enqueue we stamp the column, so a
    failed send leaves it NULL and the next hourly run retries. Channels are
    independent (a WhatsApp failure never blocks Discord) and each commits on its
    own, so one channel's stamp is durable even if the other raises. Runs off the
    event loop via asyncio.to_thread.
    """
    from chat.api import (
        enqueue_message,
        enqueue_whatsapp_message,
        whatsapp_household_group_jids,
    )
    from sqlmodel import Session

    from core.db import get_engine

    now = datetime.now(timezone.utc)
    today = now.date()

    with Session(get_engine()) as session:
        # --- Discord (enqueue uses this session; caller commits) ---
        try:
            pending = _pending(session, models.Vacancy.notified_discord, today)
            if pending:
                enqueue_message(
                    session,
                    DISCORD_CHANNEL_ID,
                    content=build_digest(_digest_dicts(pending)),
                )
                _stamp(pending, "notified_discord", now)
                session.commit()
                logger.info(
                    "dr_jobs: enqueued Discord digest for %d vacancies", len(pending)
                )
        except Exception:  # noqa: BLE001 - best-effort; NULL column retries
            session.rollback()
            logger.warning(
                "dr_jobs: Discord digest enqueue failed; will retry next run",
                exc_info=True,
            )

        # --- WhatsApp (enqueue self-commits per group; enqueue-then-stamp so a
        # failure leaves NULL rather than a silent miss) ---
        try:
            pending = _pending(session, models.Vacancy.notified_whatsapp, today)
            jids = whatsapp_household_group_jids() if pending else []
            if pending and jids:
                digest = build_digest(_digest_dicts(pending))
                for jid in jids:
                    enqueue_whatsapp_message(jid, digest)
                _stamp(pending, "notified_whatsapp", now)
                session.commit()
                logger.info(
                    "dr_jobs: enqueued WhatsApp digest for %d vacancies to %d group(s)",
                    len(pending),
                    len(jids),
                )
            # No enabled household group -> leave NULL, retry once one exists.
        except Exception:  # noqa: BLE001 - best-effort; NULL column retries
            session.rollback()
            logger.warning(
                "dr_jobs: WhatsApp digest enqueue failed; will retry next run",
                exc_info=True,
            )


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

    # Notification is driven by the row's notified_* columns, not this run's
    # inserts: seed rows were stamped in _persist, so this only ever announces
    # genuinely-new, still-open postings, and retries any prior failed send.
    await asyncio.to_thread(_notify_pending_sync)

    return None
