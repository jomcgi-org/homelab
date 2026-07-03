"""Morning digest for the WhatsApp household groups (ADR 039 spec section 5d).

A scheduled job (an Argo CronWorkflow, run hourly via ``app/jobs_main.py
whatsapp-morning-digest``) renders today's calendar events, open calendar drafts,
and open reminders into ONE ``chat.whatsapp_outbox`` message per enabled group,
then stamps ``last_digest_at`` and marks the delivered reminders. Cadence and
quiet hours come from ``chat.whatsapp_group.digest_config`` (JSONB); the job
honours quiet hours (it does not send during them, deferring to the first waking
hour) and dedupes to at most one digest per local day.

``digest_config`` shape (all optional; sensible defaults applied):
  {"send_at": "08:00", "quiet_start": "22:00", "quiet_end": "07:00"}

Reused: today's calendar events come from ``home.schedule.get_today_events`` (the
existing iCalendar snapshot), so the digest does not add a second calendar read.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from chat.models import WhatsappCalendarDraft, WhatsappGroup, WhatsappReminder
from chat.whatsapp_outbox import enqueue_message

logger = logging.getLogger(__name__)

_TZ_NAME = os.environ.get("WHATSAPP_TZ", "America/Vancouver")

_DEFAULT_SEND_AT = (8, 0)
_DEFAULT_QUIET_START = (22, 0)
_DEFAULT_QUIET_END = (7, 0)


def _tz() -> ZoneInfo:
    return ZoneInfo(_TZ_NAME)


def _utcnow() -> datetime:
    """Current UTC time, wrapped so tests can freeze the digest's clock."""
    return datetime.now(timezone.utc)


def _hhmm(value: str | None, default: tuple[int, int]) -> tuple[int, int]:
    """Parse a "HH:MM" config string into (hour, minute), or the default."""
    if not value:
        return default
    try:
        h, m = value.split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return default


def _in_quiet_hours(now_local: datetime, cfg: dict) -> bool:
    """Whether ``now_local`` falls inside the group's quiet hours.

    Quiet hours are an overnight window by default (22:00-07:00): if start > end
    the window wraps midnight, so a time is quiet when it is at/after the start OR
    before the end. A same-day window (start < end) is quiet strictly between.
    """
    start = _hhmm(cfg.get("quiet_start"), _DEFAULT_QUIET_START)
    end = _hhmm(cfg.get("quiet_end"), _DEFAULT_QUIET_END)
    cur = (now_local.hour, now_local.minute)
    if start > end:  # overnight wrap
        return cur >= start or cur < end
    return start <= cur < end


def _before_send_time(now_local: datetime, cfg: dict) -> bool:
    send_at = _hhmm(cfg.get("send_at"), _DEFAULT_SEND_AT)
    return (now_local.hour, now_local.minute) < send_at


def _already_sent_today(
    group: WhatsappGroup, now_local: datetime, tz: ZoneInfo
) -> bool:
    if group.last_digest_at is None:
        return False
    last = group.last_digest_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last.astimezone(tz).date() == now_local.date()


def _open_reminders(session: Session, group_jid: str, now_utc: datetime):
    """Undelivered reminders for the group due at or before ``now_utc``."""
    return list(
        session.exec(
            select(WhatsappReminder)
            .where(WhatsappReminder.group_jid == group_jid)
            .where(WhatsappReminder.delivered_at.is_(None))
            .where(WhatsappReminder.due_at <= now_utc)
            .order_by(WhatsappReminder.due_at)
        )
    )


def _open_drafts(session: Session, group_jid: str):
    return list(
        session.exec(
            select(WhatsappCalendarDraft)
            .where(WhatsappCalendarDraft.group_jid == group_jid)
            .where(WhatsappCalendarDraft.confirmed_at.is_(None))
            .order_by(WhatsappCalendarDraft.start_at)
        )
    )


def _today_events(session: Session):
    """Today's snapshotted iCalendar events, degrading to [] if unavailable."""
    try:
        from home.schedule import get_today_events

        return get_today_events(session)
    except Exception:
        logger.exception("whatsapp digest: calendar snapshot read failed")
        return []


def render_digest(
    session: Session, group_jid: str, now_utc: datetime, tz: ZoneInfo
) -> tuple[str, list[WhatsappReminder]]:
    """Render the digest text and return it with the reminders it included.

    The returned reminder list is what the caller marks delivered, so the digest
    stamps exactly the rows it surfaced (not any that arrive between render and
    commit).
    """
    lines = ["\U0001f305 Good morning! Here's today's digest."]

    events = _today_events(session)
    if events:
        lines.append("")
        lines.append("Calendar:")
        for ev in events:
            t = ev.get("time")
            title = ev.get("title", "")
            lines.append(f"  • {t + ' ' if t else ''}{title}".rstrip())

    drafts = _open_drafts(session, group_jid)
    if drafts:
        lines.append("")
        lines.append("Proposed (reply to confirm):")
        for d in drafts:
            when = d.start_at
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            lines.append(f"  • {when.astimezone(tz).strftime('%H:%M')} {d.title}")

    reminders = _open_reminders(session, group_jid, now_utc)
    if reminders:
        lines.append("")
        lines.append("Reminders:")
        for r in reminders:
            lines.append(f"  • {r.text}")

    if not events and not drafts and not reminders:
        lines.append("")
        lines.append("Nothing on the calendar and no open reminders. Have a good one.")

    return "\n".join(lines), reminders


def _process_group(session: Session, group: WhatsappGroup, now_utc: datetime) -> bool:
    """Send the digest for one group if it is due; return whether it was sent."""
    tz = _tz()
    now_local = now_utc.astimezone(tz)
    cfg = group.digest_config or {}

    if _in_quiet_hours(now_local, cfg):
        return False  # defer: do not send during quiet hours
    if _before_send_time(now_local, cfg):
        return False  # too early; wait for the configured send time
    if _already_sent_today(group, now_local, tz):
        return False

    text, reminders = render_digest(session, group.group_jid, now_utc, tz)
    enqueue_message(session, group.group_jid, content=text)
    for r in reminders:
        r.delivered_at = now_utc
        session.add(r)
    group.last_digest_at = now_utc
    session.add(group)
    session.commit()
    logger.info(
        "whatsapp digest: sent to %s (%d reminders)", group.group_jid, len(reminders)
    )
    return True


async def morning_digest_handler(session: Session) -> None:
    """Scheduler handler: send each enabled group's digest if it is due.

    One-shot, run hourly so a group whose send time or quiet hours have just
    lifted still gets its digest that day. Idempotent per local day via
    ``last_digest_at``.
    """
    now_utc = _utcnow()
    groups = list(
        session.exec(select(WhatsappGroup).where(WhatsappGroup.enabled == True))  # noqa: E712
    )
    for group in groups:
        try:
            _process_group(session, group, now_utc)
        except Exception:
            logger.exception("whatsapp digest: failed for group %s", group.group_jid)
            session.rollback()
