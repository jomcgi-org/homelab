"""Durable Discord scheduled tasks with leased, occurrence-safe delivery.

The database transaction boundary is deliberate: a claim is committed before
any digest generation, then finalization atomically enqueues one outbox row and
advances the task.  A restart before finalization leaves a stale lease that can
be reclaimed; a restart after finalization sees the durable occurrence and the
unique outbox dedupe key instead of firing it again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, update
from sqlmodel import Session, select

from chat.digest import digest_window
from chat.models import Message, ScheduledTask, ScheduledTaskOccurrence
from chat.outbox import enqueue_message

logger = logging.getLogger("monolith.chat.scheduled_tasks")

MAX_PENDING_PER_USER = 10
MAX_HORIZON_DAYS = 366
CLAIM_LEASE_SECONDS = 300
CLAIM_BATCH = 20
POLL_INTERVAL_SECONDS = 15.0
MAX_GENERATION_ATTEMPTS = 5
_MAX_CONTENT_CHARS = 1800
_DISCORD_MESSAGE_LIMIT = 2000
_TRUNCATION_SUFFIX = "... (truncated)"
_CRON_SEARCH_DAYS = 366 * 5


@dataclass(frozen=True)
class ClaimedOccurrence:
    occurrence_id: str
    task_id: int
    task_kind: str
    channel_id: str
    author_id: str
    payload_json: str
    cron_expression: str | None
    scheduled_for: datetime
    claim_token: str


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _occurrence_id(task_id: int, scheduled_for: datetime) -> str:
    identity = f"{task_id}:{_aware(scheduled_for).isoformat()}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _clip_scheduled_content(content: str) -> str:
    """Fit proactive content within Discord's single-message limit."""
    if len(content) <= _DISCORD_MESSAGE_LIMIT:
        return content
    return content[: _DISCORD_MESSAGE_LIMIT - len(_TRUNCATION_SUFFIX)] + (
        _TRUNCATION_SUFFIX
    )


def _parse_cron_field(field: str, minimum: int, maximum: int) -> set[int]:
    if not field:
        raise ValueError("empty cron field")
    values: set[int] = set()
    for item in field.split(","):
        if not item:
            raise ValueError("empty cron list item")
        base, separator, step_text = item.partition("/")
        if separator:
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ValueError("cron step must be an integer") from exc
            if step <= 0:
                raise ValueError("cron step must be positive")
        else:
            step = 1
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ValueError("cron ranges must be numeric") from exc
            if start > end:
                raise ValueError("cron range start must not exceed its end")
        else:
            if separator:
                raise ValueError("cron steps require '*' or a range")
            try:
                start = end = int(base)
            except ValueError as exc:
                raise ValueError("cron fields must be numeric") from exc
        if start < minimum or end > maximum:
            raise ValueError(f"cron value must be between {minimum} and {maximum}")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expression: str) -> tuple[set[int], ...]:
    """Parse a five-field UTC cron expression.

    Supports numeric values, lists, ranges and steps.  Day-of-week accepts 0
    and 7 for Sunday.  Month/day names and implementation-specific extensions
    are rejected so persisted schedules have stable semantics.
    """
    fields = expression.strip().split()
    if len(fields) != 5:
        raise ValueError("cron_expression must have exactly five fields")
    minute = _parse_cron_field(fields[0], 0, 59)
    hour = _parse_cron_field(fields[1], 0, 23)
    day = _parse_cron_field(fields[2], 1, 31)
    month = _parse_cron_field(fields[3], 1, 12)
    weekday = _parse_cron_field(fields[4], 0, 7)
    if 7 in weekday:
        weekday.add(0)
        weekday.discard(7)
    return minute, hour, day, month, weekday


def next_cron_at(expression: str, after: datetime) -> datetime:
    """Return the first matching UTC minute strictly after ``after``."""
    minute, hour, day, month, weekday = parse_cron(expression)
    raw_fields = expression.strip().split()
    day_wildcard = raw_fields[2] == "*"
    weekday_wildcard = raw_fields[4] == "*"
    candidate = _aware(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
    deadline = candidate + timedelta(days=_CRON_SEARCH_DAYS)
    while candidate <= deadline:
        # Python Monday=0; cron Sunday=0.
        cron_weekday = (candidate.weekday() + 1) % 7
        day_match = candidate.day in day
        weekday_match = cron_weekday in weekday
        if day_wildcard:
            calendar_match = weekday_match
        elif weekday_wildcard:
            calendar_match = day_match
        else:
            calendar_match = day_match or weekday_match
        if (
            candidate.minute in minute
            and candidate.hour in hour
            and candidate.month in month
            and calendar_match
        ):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("cron_expression has no occurrence within five years")


def _active_count(session: Session, author_id: str) -> int:
    return len(
        session.exec(
            select(ScheduledTask)
            .where(ScheduledTask.author_id == author_id)
            .where(ScheduledTask.status.in_(["pending", "claimed"]))
        ).all()
    )


def create_task(
    session: Session,
    *,
    channel_id: str,
    author_id: str,
    task_kind: str,
    schedule_kind: str,
    now: datetime | None = None,
    due_at: datetime | None = None,
    cron_expression: str | None = None,
    content: str = "",
    digest_mode: str = "summary",
) -> ScheduledTask | str:
    """Validate and stage a scheduled task. The caller commits."""
    now = _aware(now or datetime.now(timezone.utc))
    if not channel_id or not author_id:
        return "scheduled tasks require an authorized channel and user"
    if _active_count(session, author_id) >= MAX_PENDING_PER_USER:
        return f"you already have {MAX_PENDING_PER_USER} pending scheduled tasks"

    payload: dict[str, str]
    next_run_at: datetime
    normalized_cron: str | None = None
    normalized_due_at: datetime | None = None
    if task_kind == "reminder" and schedule_kind == "one_shot":
        content = content.strip()
        if not content:
            return "reminder text must not be empty"
        if len(content) > _MAX_CONTENT_CHARS:
            return f"reminder text cannot exceed {_MAX_CONTENT_CHARS} characters"
        if due_at is None:
            return "due_at is required for a one-shot reminder"
        next_run_at = _aware(due_at)
        normalized_due_at = next_run_at
        if next_run_at <= now:
            return "due_at must be in the future"
        if next_run_at > now + timedelta(days=MAX_HORIZON_DAYS):
            return f"due_at cannot be more than {MAX_HORIZON_DAYS} days out"
        payload = {"content": content}
    elif task_kind == "digest" and schedule_kind == "cron":
        if digest_mode not in {"summary", "decisions"}:
            return "digest_mode must be 'summary' or 'decisions'"
        if not cron_expression:
            return "cron_expression is required for a recurring digest"
        normalized_cron = " ".join(cron_expression.strip().split())
        try:
            next_run_at = next_cron_at(normalized_cron, now)
        except ValueError as exc:
            return str(exc)
        payload = {"mode": digest_mode}
    else:
        return "only one-shot reminders and recurring cron digests are supported"

    task = ScheduledTask(
        channel_id=channel_id,
        author_id=author_id,
        task_kind=task_kind,
        schedule_kind=schedule_kind,
        payload_json=json.dumps(payload, sort_keys=True),
        due_at=normalized_due_at,
        cron_expression=normalized_cron,
        next_run_at=next_run_at,
    )
    session.add(task)
    return task


def list_tasks(
    session: Session, author_id: str, channel_id: str | None = None
) -> list[ScheduledTask]:
    statement = (
        select(ScheduledTask)
        .where(ScheduledTask.author_id == author_id)
        .where(ScheduledTask.status.in_(["pending", "claimed"]))
    )
    if channel_id is not None:
        statement = statement.where(ScheduledTask.channel_id == channel_id)
    return list(
        session.exec(statement.order_by(ScheduledTask.next_run_at, ScheduledTask.id))
    )


def cancel_task(
    session: Session,
    author_id: str,
    task_id: int,
    *,
    channel_id: str | None = None,
    task_kind: str | None = None,
) -> bool:
    """Cancel an owned task unless an occurrence is already claimed."""
    statement = (
        update(ScheduledTask)
        .where(ScheduledTask.id == task_id)
        .where(ScheduledTask.author_id == author_id)
        .where(ScheduledTask.status == "pending")
    )
    if channel_id is not None:
        statement = statement.where(ScheduledTask.channel_id == channel_id)
    if task_kind is not None:
        statement = statement.where(ScheduledTask.task_kind == task_kind)
    result = session.exec(
        statement.values(status="cancelled", completed_at=datetime.now(timezone.utc))
    )
    return result.rowcount == 1


def claim_due(
    session: Session,
    now: datetime,
    claimant: str,
    *,
    lease_seconds: int = CLAIM_LEASE_SECONDS,
    limit: int = CLAIM_BATCH,
) -> list[ClaimedOccurrence]:
    """Atomically claim due tasks and reclaim expired leases.

    Candidate discovery may race, but each conditional UPDATE can succeed for
    only one claimant.  The returned objects are detached value snapshots and
    are safe to use after this transaction commits.
    """
    now = _aware(now)
    stale_before = now - timedelta(seconds=lease_seconds)
    candidates = session.exec(
        select(ScheduledTask)
        .where(
            or_(
                and_(
                    ScheduledTask.status == "pending",
                    ScheduledTask.next_run_at <= now,
                ),
                and_(
                    ScheduledTask.status == "claimed",
                    ScheduledTask.next_run_at <= now,
                    or_(
                        ScheduledTask.claimed_at.is_(None),
                        ScheduledTask.claimed_at <= stale_before,
                    ),
                ),
            )
        )
        .order_by(ScheduledTask.next_run_at, ScheduledTask.id)
        .limit(limit)
    ).all()
    claimed: list[ClaimedOccurrence] = []
    for task in candidates:
        if len(claimed) >= limit or _aware(task.next_run_at) > now:
            continue
        stale = task.status == "claimed" and (
            task.claimed_at is None or _aware(task.claimed_at) <= stale_before
        )
        if task.status == "claimed" and not stale:
            continue
        scheduled_for = _aware(task.next_run_at)
        occurrence_id = _occurrence_id(task.id, scheduled_for)
        token = f"{claimant}:{uuid.uuid4().hex}"
        statement = (
            update(ScheduledTask)
            .where(ScheduledTask.id == task.id)
            .where(ScheduledTask.status == task.status)
        )
        if task.status == "claimed":
            statement = statement.where(ScheduledTask.claim_token == task.claim_token)
        result = session.exec(
            statement.values(
                status="claimed",
                claim_token=token,
                claimed_at=now,
                current_occurrence_id=occurrence_id,
            )
        )
        if result.rowcount != 1:
            continue
        occurrence = session.get(ScheduledTaskOccurrence, occurrence_id)
        if occurrence is None:
            occurrence = ScheduledTaskOccurrence(
                occurrence_id=occurrence_id,
                scheduled_task_id=task.id,
                scheduled_for=scheduled_for,
                claim_token=token,
                claimed_at=now,
            )
        else:
            occurrence.status = "claimed"
            occurrence.claim_token = token
            occurrence.claimed_at = now
            occurrence.last_error = None
        session.add(occurrence)
        claimed.append(
            ClaimedOccurrence(
                occurrence_id=occurrence_id,
                task_id=task.id,
                task_kind=task.task_kind,
                channel_id=task.channel_id,
                author_id=task.author_id,
                payload_json=task.payload_json,
                cron_expression=task.cron_expression,
                scheduled_for=scheduled_for,
                claim_token=token,
            )
        )
    return claimed


def finalize_claim(
    session: Session,
    claim: ClaimedOccurrence,
    content: str,
    now: datetime,
) -> bool:
    """Atomically enqueue a claimed occurrence and complete/advance its task."""
    now = _aware(now)
    task = session.get(ScheduledTask, claim.task_id)
    occurrence = session.get(ScheduledTaskOccurrence, claim.occurrence_id)
    if (
        task is None
        or occurrence is None
        or task.status != "claimed"
        or task.claim_token != claim.claim_token
        or occurrence.claim_token != claim.claim_token
    ):
        return False
    enqueue_message(
        session,
        claim.channel_id,
        content=_clip_scheduled_content(content),
        kind="scheduled_task",
        payload={"occurrence_id": claim.occurrence_id, "task_id": claim.task_id},
        dedupe_key=f"scheduled:{claim.occurrence_id}",
    )
    session.flush()
    from chat.models import DiscordOutbox

    outbox = session.exec(
        select(DiscordOutbox).where(
            DiscordOutbox.dedupe_key == f"scheduled:{claim.occurrence_id}"
        )
    ).one()
    occurrence.status = "enqueued"
    occurrence.outbox_id = outbox.id
    occurrence.enqueued_at = now
    if task.schedule_kind == "one_shot":
        task.status = "completed"
        task.completed_at = now
    else:
        task.status = "pending"
        # Skip missed backlog after downtime while preserving the completed
        # occurrence's exact scheduled identity.
        task.next_run_at = next_cron_at(task.cron_expression or "", now)
    task.claim_token = None
    task.claimed_at = None
    task.current_occurrence_id = None
    task.failure_count = 0
    session.add(task)
    session.add(occurrence)
    return True


def release_claim(
    session: Session,
    claim: ClaimedOccurrence,
    error: str,
    now: datetime | None = None,
) -> bool:
    """Release a failed generation without retrying one occurrence forever."""
    task = session.get(ScheduledTask, claim.task_id)
    occurrence = session.get(ScheduledTaskOccurrence, claim.occurrence_id)
    if (
        task is None
        or task.status != "claimed"
        or task.claim_token != claim.claim_token
    ):
        return False
    now = _aware(now or datetime.now(timezone.utc))
    task.failure_count += 1
    if task.failure_count < MAX_GENERATION_ATTEMPTS:
        task.status = "pending"
    elif task.schedule_kind == "cron":
        # The retry counter belongs to one scheduled occurrence. Once it is
        # exhausted, skip that occurrence and let the next cron instant try
        # with a fresh budget instead of calling the model every poll forever.
        task.status = "pending"
        task.next_run_at = next_cron_at(task.cron_expression or "", now)
        task.failure_count = 0
    else:
        task.status = "failed"
        task.completed_at = now
    task.claim_token = None
    task.claimed_at = None
    task.current_occurrence_id = None
    if occurrence is not None and occurrence.claim_token == claim.claim_token:
        occurrence.status = "failed"
        occurrence.last_error = error[:500]
        session.add(occurrence)
    session.add(task)
    return True


def _claim_with_engine(engine, now: datetime, claimant: str) -> list[ClaimedOccurrence]:
    with Session(engine) as session:
        claims = claim_due(session, now, claimant)
        session.commit()
        return claims


def _finalize_with_engine(
    engine, claim: ClaimedOccurrence, content: str, now: datetime
) -> bool:
    with Session(engine) as session:
        result = finalize_claim(session, claim, content, now)
        session.commit()
        return result


def _release_with_engine(
    engine, claim: ClaimedOccurrence, error: str, now: datetime
) -> None:
    with Session(engine) as session:
        release_claim(session, claim, error, now)
        session.commit()


def _fetch_digest_window(engine, channel_id: str) -> list[Message]:
    with Session(engine) as session:
        newest = list(
            session.exec(
                select(Message)
                .where(Message.channel_id == channel_id)
                .order_by(Message.created_at.desc())
                .limit(300)
            ).all()
        )
        window: list[Message] = []
        chars = 0
        for message in newest:
            chars += len(message.content)
            if chars > 30_000 and window:
                break
            window.append(message)
        window.reverse()
        return window


async def _default_digest_builder(engine, claim: ClaimedOccurrence) -> str:
    from chat.summarizer import build_llm_caller

    payload = json.loads(claim.payload_json)
    window = await asyncio.to_thread(_fetch_digest_window, engine, claim.channel_id)
    digest = await digest_window(window, payload["mode"], build_llm_caller())
    return f"📋 Scheduled {payload['mode']} digest:\n{digest}"


async def drain_once(
    engine,
    *,
    now: datetime | None = None,
    claimant: str | None = None,
    digest_builder: Callable[[object, ClaimedOccurrence], Awaitable[str]] | None = None,
) -> int:
    """Claim and enqueue due work once. Returns finalized occurrence count."""
    now = _aware(now or datetime.now(timezone.utc))
    claimant = claimant or uuid.uuid4().hex
    claims = await asyncio.to_thread(_claim_with_engine, engine, now, claimant)
    finalized = 0
    for claim in claims:
        try:
            payload = json.loads(claim.payload_json)
            if claim.task_kind == "reminder":
                content = f"⏰ <@{claim.author_id}> reminder: {payload['content']}"
            else:
                builder = digest_builder or _default_digest_builder
                content = await builder(engine, claim)
            if await asyncio.to_thread(
                _finalize_with_engine, engine, claim, content, now
            ):
                finalized += 1
        except Exception as exc:
            logger.exception("scheduled task occurrence %s failed", claim.occurrence_id)
            await asyncio.to_thread(_release_with_engine, engine, claim, str(exc), now)
    return finalized


async def run_scheduler(engine, poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
    """Leader lifecycle loop for persisted scheduled tasks."""
    instance = uuid.uuid4().hex
    logger.info("Discord scheduled-task drain started (poll=%.1fs)", poll_interval)
    while True:
        try:
            await drain_once(engine, claimant=instance)
        except Exception:
            logger.exception("scheduled-task drain tick failed")
        await asyncio.sleep(poll_interval)
