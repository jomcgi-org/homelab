"""Leader-owned export of finished Ember sessions to knowledge raws."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os

from sqlalchemy import exists, text
from sqlalchemy.orm import defer
from sqlmodel import Session, func, select

from agent_sessions.constants import KG_NODE_KEY, SYNTHETIC_SESSION_PREFIX
from agent_sessions.models import AgentSession, AgentTurn, PendingMessage
from agent_sessions.rationale import parse_rationale
from agent_sessions.redact import redact_text
from core.db import get_engine
from framework import log_task_exception
from knowledge.api import enqueue_extraction, ingest_raw_with_status

logger = logging.getLogger(__name__)

KG_FEED_INTERVAL_SECONDS = 60
KG_FEED_BATCH = 3
KG_FEED_QUIET_SECONDS = 1800
PROMPT_CAP = 8 * 1024
RESULT_CAP = 12 * 1024
DOCUMENT_CAP = 400 * 1024
PROCESS_STARTED_AT = datetime.now(timezone.utc)

_kg_feed_task: asyncio.Task | None = None


def _enabled() -> bool:
    return os.environ.get("KG_FEED_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _since_floor() -> datetime:
    configured = os.environ.get("KG_FEED_SINCE", "").strip()
    if not configured:
        return PROCESS_STARTED_AT
    floor = datetime.fromisoformat(configured.replace("Z", "+00:00"))
    if floor.tzinfo is None:
        raise ValueError("KG_FEED_SINCE must be an RFC3339 timestamp with timezone")
    return floor.astimezone(timezone.utc)


def _clip_text(value: str | None, limit: int) -> str:
    encoded = str(value or "").encode("utf-8")
    if len(encoded) <= limit:
        return encoded.decode("utf-8")
    return encoded[:limit].decode("utf-8", "ignore")


def _size(value: str) -> int:
    return len(value.encode("utf-8"))


def pick_finished_sessions(
    session: Session, limit: int = KG_FEED_BATCH
) -> list[tuple[AgentSession, int]]:
    """Return quiet finished sessions with turns beyond their KG watermark."""
    max_seq = (
        select(AgentTurn.session_id, func.max(AgentTurn.seq).label("max_seq"))
        .group_by(AgentTurn.session_id)
        .subquery()
    )
    quiet_before = datetime.now(timezone.utc) - timedelta(seconds=KG_FEED_QUIET_SECONDS)
    pending = exists().where(PendingMessage.session_id == AgentSession.id)
    rows = session.exec(
        select(AgentSession, max_seq.c.max_seq)
        .join(max_seq, max_seq.c.session_id == AgentSession.id)
        .where(AgentSession.status.in_(("completed", "warn")))
        .where(~pending)
        .where(AgentSession.created_at >= _since_floor())
        .where(AgentSession.last_turn_at < quiet_before)
        .where(AgentSession.node_key.is_distinct_from(KG_NODE_KEY))
        .where(~AgentSession.local_session_id.startswith(SYNTHETIC_SESSION_PREFIX))
        .where(max_seq.c.max_seq > func.coalesce(AgentSession.kg_extracted_turn_seq, 0))
        .order_by(AgentSession.last_turn_at.desc())
        .limit(limit)
    ).all()
    return [(row, int(latest_seq)) for row, latest_seq in rows]


def _clip_result(value: str | None) -> str:
    text = str(value or "")
    if _size(text) <= RESULT_CAP:
        return text
    rationale = parse_rationale(text).get("raw")
    if not rationale or _size(rationale) >= RESULT_CAP - 2:
        return _clip_text(text, RESULT_CAP)
    prefix = _clip_text(text, RESULT_CAP - _size(rationale) - 2).rstrip()
    return f"{prefix}\n\n{rationale}"


def _turn_block(turn: AgentTurn) -> str:
    parts = [
        f"## Turn {turn.seq}",
        "",
        "**Prompt:**",
        "",
        _clip_text(turn.prompt, PROMPT_CAP),
        "",
        "**Result:**",
        "",
        _clip_result(turn.result_text),
    ]
    if turn.commit_sha or turn.base_sha:
        parts.extend(
            [
                "",
                f"commit: {turn.commit_sha or ''} base: {turn.base_sha or ''}",
            ]
        )
    return "\n".join(parts)


def _scalar(value: object) -> str:
    if isinstance(value, int):
        return str(value)
    text = _clip_text("" if value is None else str(value), 4096)
    return json.dumps(text, ensure_ascii=False)


def _timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _title(session_row: AgentSession, turns: list[AgentTurn]) -> str:
    if session_row.title:
        return session_row.title[:80]
    if not turns:
        return f"Ember session {session_row.id}"
    first_line = str(turns[0].prompt or "").splitlines()[0].strip()
    return (first_line or f"Ember session {session_row.id}")[:80]


def _frontmatter(
    session_row: AgentSession,
    turns: list[AgentTurn],
    *,
    truncated: bool,
) -> str:
    first_seq = turns[0].seq
    last_seq = turns[-1].seq
    scope = (
        f"repo:{session_row.repo}" if session_row.repo else f"session:{session_row.id}"
    )
    fields = (
        ("title", _title(session_row, turns)),
        ("provider", "ember"),
        ("session_id", session_row.id),
        ("local_session_id", session_row.local_session_id),
        ("workflow_id", session_row.workflow_id),
        ("node_key", session_row.node_key),
        ("repo", session_row.repo),
        ("branch", session_row.branch),
        ("model", session_row.model),
        ("triggered_by", session_row.triggered_by),
        ("scope", scope),
        ("turn_range", f"{first_seq}-{last_seq}"),
        ("started_at", _timestamp(session_row.created_at)),
        ("ended_at", _timestamp(session_row.last_turn_at)),
    )
    lines = ["---", *(f"{key}: {_scalar(value)}" for key, value in fields)]
    lines.append(f"truncated: {'true' if truncated else 'false'}")
    lines.extend(["---", ""])
    return "\n".join(lines)


def _render_session_raw(
    session_row: AgentSession, turns: list[AgentTurn]
) -> tuple[str, int, bool]:
    if not turns:
        raise ValueError("cannot render a session without turns")
    ordered = sorted(turns, key=lambda turn: turn.seq)
    header, header_redactions = redact_text(
        _frontmatter(session_row, ordered, truncated=False)
    )
    blocks = [redact_text(_turn_block(turn)) for turn in ordered]
    content = header + "\n\n".join(block for block, _ in blocks)
    redactions = header_redactions + sum(count for _, count in blocks)
    truncated = _size(content) > DOCUMENT_CAP
    if truncated:
        header, header_redactions = redact_text(
            _frontmatter(session_row, ordered, truncated=True)
        )
        kept = list(blocks)
        omitted = 0
        while True:
            split = (len(kept) + 1) // 2
            marker = f"\n\n<!-- {omitted} turns elided -->\n\n"
            content = (
                header
                + "\n\n".join(block for block, _ in kept[:split])
                + marker
                + "\n\n".join(block for block, _ in kept[split:])
            )
            if _size(content) <= DOCUMENT_CAP or not kept:
                redactions = header_redactions + sum(count for _, count in kept)
                break
            middle = len(kept) // 2
            kept.pop(middle)
            omitted += 1
    return content, redactions, truncated


def render_session_raw(session_row: AgentSession, turns: list[AgentTurn]) -> str:
    """Render an incremental session transcript as bounded markdown."""
    content, _, _ = _render_session_raw(session_row, turns)
    return content


def _raw_is_queued_or_handled(session: Session, raw_id: str) -> bool:
    dialect = session.get_bind().dialect.name
    jobs_table = "routine_jobs" if dialect == "sqlite" else "claude_agent.routine_jobs"
    raw_table = "raw_inputs" if dialect == "sqlite" else "knowledge.raw_inputs"
    provenance_table = (
        "atom_raw_provenance"
        if dialect == "sqlite"
        else "knowledge.atom_raw_provenance"
    )
    queued = session.execute(
        text(f"SELECT 1 FROM {jobs_table} WHERE name = :name LIMIT 1"),
        {"name": f"kg:{raw_id}"},
    ).first()
    if queued is not None:
        return True
    handled = session.execute(
        text(
            f"""
            SELECT 1
              FROM {provenance_table} AS provenance
              JOIN {raw_table} AS raw ON raw.id = provenance.raw_fk
             WHERE raw.raw_id = :raw_id
               AND (provenance.derived_note_id IS NULL
                    OR provenance.derived_note_id <> 'failed')
             LIMIT 1
            """
        ),
        {"raw_id": raw_id},
    ).first()
    return handled is not None


def _feed_once_sync(ingest, enqueue, is_handled) -> int:
    with Session(get_engine()) as session:
        candidates = pick_finished_sessions(session, KG_FEED_BATCH)
        fed = 0
        for candidate, latest_seq in candidates:
            try:
                watermark = candidate.kg_extracted_turn_seq or 0
                turns = list(
                    session.exec(
                        select(AgentTurn)
                        .options(
                            defer(AgentTurn.diff_blob),
                            defer(AgentTurn.artifact_blob),
                        )
                        .where(AgentTurn.session_id == candidate.id)
                        .where(AgentTurn.seq > watermark)
                        .where(AgentTurn.seq <= latest_seq)
                        .order_by(AgentTurn.seq)
                    ).all()
                )
                if not turns:
                    continue
                content, redactions, truncated = _render_session_raw(candidate, turns)
                turn_range = f"{turns[0].seq}-{turns[-1].seq}"
                raw, created = ingest(
                    session,
                    content=content,
                    source="ember-session",
                    original_url=f"ember-session:{candidate.id}",
                    extra={
                        "session_id": candidate.id,
                        "local_session_id": candidate.local_session_id,
                        "workflow_id": candidate.workflow_id,
                        "node_key": candidate.node_key,
                        "repo": candidate.repo,
                        "branch": candidate.branch,
                        "model": candidate.model,
                        "triggered_by": candidate.triggered_by,
                        "turn_range": turn_range,
                        "redactions": redactions,
                        "truncated": truncated,
                    },
                    commit=False,
                )
                # New ember-session raws are enqueued by ingest. If an earlier
                # attempt wrote the raw but missed the watermark, make sure its
                # idempotent retry is queued too.
                handled = is_handled(session, raw.raw_id)
                if not created and not handled:
                    enqueue(session, raw.raw_id, commit=False)
                    handled = is_handled(session, raw.raw_id)
                if not handled:
                    session.rollback()
                    logger.warning(
                        "KG feed left watermark unchanged for session %s: "
                        "raw %s is not queued or handled",
                        candidate.id,
                        raw.raw_id,
                    )
                    continue
                current = session.get(AgentSession, candidate.id)
                if current is None:
                    session.rollback()
                    continue
                current.kg_extracted_turn_seq = latest_seq
                session.add(current)
                session.commit()
                fed += 1
            except Exception:
                session.rollback()
                logger.exception("KG feed failed for session %s", candidate.id)
        return fed


async def feed_once(
    ingest=ingest_raw_with_status,
    enqueue=enqueue_extraction,
    is_handled=None,
) -> int:
    """Export up to KG_FEED_BATCH finished sessions."""
    if not _enabled():
        return 0
    try:
        return await asyncio.to_thread(
            _feed_once_sync,
            ingest,
            enqueue,
            is_handled or _raw_is_queued_or_handled,
        )
    except Exception:
        logger.exception("KG feed pass failed")
        return 0


async def _kg_feed_loop() -> None:
    while True:
        await asyncio.sleep(KG_FEED_INTERVAL_SECONDS)
        try:
            await feed_once()
        except Exception:
            logger.exception("KG feed pass failed")


def start_kg_feed_loop() -> list[asyncio.Task]:
    """Start the leader-owned KG feed loop and return its task."""
    global _kg_feed_task
    if _kg_feed_task is None or _kg_feed_task.done():
        _kg_feed_task = asyncio.create_task(_kg_feed_loop())
        _kg_feed_task.add_done_callback(log_task_exception)
    return [_kg_feed_task]
