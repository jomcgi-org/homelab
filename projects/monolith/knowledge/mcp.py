"""MCP tools for knowledge graph search and task tracking.

Defines note tools (``search_knowledge``, ``get_note``, ``report_knowledge``,
``dispute_fact``, ``report_distress``) and task tools (``list_tasks``,
``search_tasks``, ``update_task``, ``get_daily_tasks``, ``get_weekly_tasks``).
``knowledge.module`` registers the full catalogue on the shared private MCP
instance, while pruned entrypoints can select a safe subset. Tools call
KnowledgeStore directly (no HTTP round-trip).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import yaml
from sqlmodel import Session, select

from auth.api import current_principal
from core.db import get_engine
from knowledge.api import ingest_raw_with_status
from knowledge.atoms import index_atom
from knowledge.indexing import index_note_from_raw
from knowledge.models import Dispute
from knowledge.notes import resolve_note_body
from knowledge.redact import redact_text
from knowledge.store import KnowledgeStore
from shared.embedding import EmbeddingClient

logger = logging.getLogger(__name__)


async def _index_atom(session: Session, **kwargs) -> str:
    """Compatibility seam around the shared atom core for existing callers.

    Retained after the MCP CRUD tools were retired: knowledge/gaps.py imports
    it for the gap-answer endpoint, so it is shared production code rather
    than tool-only scaffolding.
    """
    return await index_atom(
        session,
        **kwargs,
        _store_factory=KnowledgeStore,
        _embedding_client_factory=EmbeddingClient,
        _indexer=index_note_from_raw,
    )


DEFAULT_REPO_SCOPE = os.getenv(
    "KNOWLEDGE_DEFAULT_REPO_SCOPE", "repo:jomcgi-org/homelab"
)
_PROPOSED_SCOPES = frozenset({"repo", "org", "environment", "personal", "session"})
_ASSERTION_CAP = 20_000
_DETAILS_CAP = 8_000
_EVIDENCE_ITEM_CAP = 500
_EVIDENCE_ITEMS_CAP = 20
_EVIDENCE_JOINED_CAP = 10_000
_REASON_CAP = 4_000
_VALIDITY_HINT_CAP = 200
_DISPUTED_NOTE_BODY_CAP = 8 * 1024
_NOTIFY_SUMMARY_CAP = 300
_NOTIFY_INTERVENTION_CAP = 400
_NOTIFY_MESSAGE_CAP = 1_800


def _reporter_extra(principal: Any) -> dict[str, str]:
    return {
        "reporter_subject": principal.subject,
        "reporter_authority": str(principal.authority),
        "reporter_kind": str(principal.kind),
    }


def _markdown_raw(frontmatter_values: dict[str, object], body: str) -> str:
    header = yaml.safe_dump(frontmatter_values, sort_keys=False).rstrip()
    return f"---\n{header}\n---\n\n{body.rstrip()}\n"


def _evidence_markdown(evidence: list[str] | None) -> str:
    return "\n".join(f"- {item}" for item in evidence or [])


def _evidence_error(evidence: list[str] | None) -> str | None:
    if evidence is None:
        return None
    if len(evidence) > _EVIDENCE_ITEMS_CAP:
        return f"evidence must contain at most {_EVIDENCE_ITEMS_CAP} items"
    if any(len(item) > _EVIDENCE_ITEM_CAP for item in evidence):
        return f"evidence items must not exceed {_EVIDENCE_ITEM_CAP} characters"
    if len("\n".join(evidence)) > _EVIDENCE_JOINED_CAP:
        return f"evidence must not exceed {_EVIDENCE_JOINED_CAP} joined characters"
    return None


def _resolved_scope(proposed_scope: str, subject: str) -> str:
    if proposed_scope == "repo":
        return DEFAULT_REPO_SCOPE
    if proposed_scope == "org":
        return "org:jomcgi-org"
    if proposed_scope == "environment":
        return "environment:homelab"
    if proposed_scope == "session":
        utc_date = datetime.now(timezone.utc).date().isoformat()
        return f"session:{subject}:{utc_date}"
    return f"{proposed_scope}:{subject}"


async def _notify(message: str, level: str) -> dict:
    """Send an operator notification through the shared tier boundary.

    The notification path lives in ``shared`` precisely so both the private and
    agent tiers can use it. Function-local imports into pruned domains fail at
    call time, not build time, so this deferred import must remain tier-portable.
    """

    from shared.notify import notify as _notify_impl

    return await _notify_impl(message, level)


_KNOWLEDGE_TOOLS: list = []


def _knowledge_tool(fn):
    """Mark a function as part of the knowledge MCP catalogue.

    Registration happens at the DEFINITION SITE on purpose. A separate list of
    tool names is a thing to forget, and a tool missing from it does not fail:
    it silently disappears from whichever tier reads that list, which is how
    report_knowledge stayed invisible in Context Forge for a day.
    """

    _KNOWLEDGE_TOOLS.append(fn)
    return fn


@_knowledge_tool
async def search_knowledge(
    query: str,
    limit: int = 20,
    type: str | None = None,
) -> dict:
    """Semantic search over the knowledge graph.

    Embeds the query and searches notes by cosine similarity.
    Returns ranked results with title, type, tags, best-matching
    section, a 240-char snippet, and graph edges.

    Args:
        query: Natural language search query (minimum 2 characters).
        limit: Maximum results to return (default 20, max 100).
        type: Optional note type filter (e.g. "concept", "paper").
    """
    if len(query) < 2:
        return {"results": []}

    embed_client = EmbeddingClient()
    try:
        vector = await embed_client.embed(query)
    except Exception:
        logger.exception("knowledge mcp: embedding call failed")
        return {"error": "embedding unavailable"}

    with Session(get_engine()) as session:
        results = KnowledgeStore(session).search_notes_with_context(
            query_embedding=vector,
            limit=min(limit, 100),
            type_filter=type,
        )
    return {"results": results}


@_knowledge_tool
async def get_note(note_id: str) -> dict:
    """Retrieve a knowledge note by its stable ID.

    Returns note metadata (title, type, tags), the full markdown
    body (authoritative ``knowledge.notes.content`` in Postgres), and all
    outgoing graph edges.

    Args:
        note_id: The stable note identifier (e.g. "attention-is-all-you-need").
    """
    with Session(get_engine()) as session:
        store = KnowledgeStore(session)
        note = store.get_note_by_id(note_id)
        if note is None:
            return {"error": f"note not found: {note_id}"}

        # ADR 006: body of record is Postgres ``content``.
        body = resolve_note_body(note.get("content"))
        if body is None:
            return {"error": f"note has no body: {note_id}"}

        edges = store.get_note_links(note_id)
        return {**note, "content": body, "edges": edges}


def _report_knowledge_sync(
    assertion: str,
    proposed_scope: str,
    evidence: list[str] | None,
    validity_hint: str | None,
    reporter: dict[str, str],
) -> dict:
    assertion = assertion.strip()
    if not assertion:
        return {"error": "assertion must not be empty"}
    if len(assertion) > _ASSERTION_CAP:
        return {"error": f"assertion must not exceed {_ASSERTION_CAP} characters"}
    if proposed_scope not in _PROPOSED_SCOPES:
        valid = ", ".join(sorted(_PROPOSED_SCOPES))
        return {"error": f"proposed_scope must be one of {valid}"}
    evidence_error = _evidence_error(evidence)
    if evidence_error is not None:
        return {"error": evidence_error}
    if validity_hint is not None and len(validity_hint) > _VALIDITY_HINT_CAP:
        return {
            "error": (f"validity_hint must not exceed {_VALIDITY_HINT_CAP} characters")
        }

    scope = _resolved_scope(proposed_scope, reporter["reporter_subject"])
    content = _markdown_raw(
        {
            "title": assertion[:80],
            "proposed_scope": proposed_scope,
            "scope": scope,
            "validity_hint": validity_hint,
            "reporter": reporter["reporter_subject"],
        },
        f"{assertion}\n\n## Evidence\n\n{_evidence_markdown(evidence)}",
    )
    extra = {
        **reporter,
        "proposed_scope": proposed_scope,
        "scope": scope,
        "validity_hint": validity_hint,
    }

    with Session(get_engine()) as session:
        raw, created = ingest_raw_with_status(
            session,
            content=content,
            source="agent-report",
            original_url=None,
            extra=extra,
        )
        return {
            "raw_id": raw.raw_id,
            "created": created,
            "status": "queued" if created else "duplicate",
            "scope": scope,
        }


@_knowledge_tool
async def report_knowledge(
    assertion: str,
    proposed_scope: str = "repo",
    evidence: list[str] | None = None,
    validity_hint: str | None = None,
) -> dict:
    """Report an unverified assertion for grounded knowledge extraction.

    Reports are unverified evidence. They never become facts without the
    extraction process checking and classifying them. Session scope currently
    resolves to ``session:<subject>:<UTC YYYY-MM-DD>``. A per-session ID arrives
    with #5569.

    Args:
        assertion: The claim to report, limited to 20,000 characters.
        proposed_scope: One of repo, org, environment, personal, or session.
        evidence: Optional references or observations supporting the claim.
        validity_hint: Optional description of when the claim is valid.
    """
    reporter = _reporter_extra(current_principal())
    return await asyncio.to_thread(
        _report_knowledge_sync,
        assertion,
        proposed_scope,
        evidence,
        validity_hint,
        reporter,
    )


def _dispute_fact_sync(
    fact_id: str,
    reason: str,
    evidence: list[str] | None,
    reporter: dict[str, str],
) -> dict:
    reason = reason.strip()
    if not reason:
        return {"error": "reason must not be empty"}
    reason = reason[:_REASON_CAP]
    evidence_error = _evidence_error(evidence)
    if evidence_error is not None:
        return {"error": evidence_error}

    with Session(get_engine()) as session:
        note = KnowledgeStore(session).get_note_by_id(fact_id)
        if note is None:
            return {"error": "unknown fact"}

        existing_dispute = session.exec(
            select(Dispute).where(
                Dispute.note_id == fact_id,
                Dispute.reporter_subject == reporter["reporter_subject"],
                Dispute.reason == reason,
                Dispute.state == "open",
            )
        ).first()
        if existing_dispute is not None:
            return {
                "dispute_id": existing_dispute.id,
                "note_id": fact_id,
                "status": "already-disputed",
                "raw_id": existing_dispute.raw_id,
            }

        quoted_title = "\n".join(
            f"> {line}" for line in str(note.get("title") or fact_id).splitlines()
        )
        note_body = str(note.get("content") or "")[:_DISPUTED_NOTE_BODY_CAP]
        quoted_body = "\n".join(f"> {line}" for line in note_body.splitlines())
        content = _markdown_raw(
            {
                "title": f"Dispute: {str(note.get('title') or fact_id)[:80]}",
                "note_id": fact_id,
                "reporter": reporter["reporter_subject"],
                "dispute_nonce": str(uuid4()),
            },
            (
                "## Current fact\n\n"
                f"{quoted_title}\n\n{quoted_body}\n\n"
                f"## Reason\n\n{reason}\n\n"
                f"## Evidence\n\n{_evidence_markdown(evidence)}"
            ),
        )
        try:
            raw, _ = ingest_raw_with_status(
                session,
                content=content,
                source="dispute",
                original_url=None,
                extra={"note_id": fact_id, **reporter},
                commit=False,
            )
            raw_id = raw.raw_id
            dispute = Dispute(
                note_id=fact_id,
                raw_id=raw_id,
                reason=reason,
                evidence=evidence or [],
                reporter_subject=reporter["reporter_subject"],
                reporter_authority=reporter["reporter_authority"],
                state="open",
            )
            session.add(dispute)
            session.flush()
            dispute_id = dispute.id
            session.commit()
        except Exception:
            session.rollback()
            raise

        return {
            "dispute_id": dispute_id,
            "note_id": fact_id,
            "status": "disputed",
            "raw_id": raw_id,
        }


@_knowledge_tool
async def dispute_fact(
    fact_id: str,
    reason: str,
    evidence: list[str] | None = None,
) -> dict:
    """Dispute a live fact without deleting or editing the note.

    The open dispute is visible immediately in knowledge search. Its raw
    evidence is queued for extraction to confirm, narrow, or reject the claim.

    Args:
        fact_id: Stable ID of the live knowledge note being disputed.
        reason: Explanation of why the current fact may be wrong.
        evidence: Optional references or observations supporting the dispute.
    """
    reporter = _reporter_extra(current_principal())
    return await asyncio.to_thread(
        _dispute_fact_sync, fact_id, reason, evidence, reporter
    )


def _report_distress_sync(
    summary: str,
    severity: str,
    details: str,
    requested_intervention: str,
    reporter: dict[str, str],
) -> tuple[dict, str | None, str | None]:
    if severity not in {"blocked", "degraded", "urgent"}:
        return (
            {"error": "severity must be one of blocked, degraded, urgent"},
            None,
            None,
        )
    if len(details) > _DETAILS_CAP:
        return (
            {"error": f"details must not exceed {_DETAILS_CAP} characters"},
            None,
            None,
        )

    content = _markdown_raw(
        {
            "title": f"Distress: {summary[:80]}",
            "severity": severity,
            "reporter": reporter["reporter_subject"],
        },
        (
            f"## Summary\n\n{summary}\n\n"
            f"## Severity\n\n{severity}\n\n"
            f"## Details\n\n{details}\n\n"
            "## Requested intervention\n\n"
            f"{requested_intervention}"
        ),
    )
    with Session(get_engine()) as session:
        raw, _ = ingest_raw_with_status(
            session,
            content=content,
            source="distress",
            original_url=None,
            extra={
                "severity": severity,
                "requested_intervention": requested_intervention,
                **reporter,
            },
        )
        raw_id = raw.raw_id

    notify_summary, _ = redact_text(summary)
    notify_summary = notify_summary[:_NOTIFY_SUMMARY_CAP]
    notify_intervention, _ = redact_text(requested_intervention)
    notify_intervention = notify_intervention[:_NOTIFY_INTERVENTION_CAP]
    message = (
        f"raw {raw_id} | distress({severity}) from "
        f"{reporter['reporter_subject']}: {notify_summary}"
        f" | wants: {notify_intervention}"
    )
    message, _ = redact_text(message)
    message = message[:_NOTIFY_MESSAGE_CAP]
    level = "error" if severity == "urgent" else "warn"
    return ({"intervention_id": raw_id}, message, level)


@_knowledge_tool
async def report_distress(
    summary: str,
    severity: str,
    details: str = "",
    requested_intervention: str = "",
) -> dict:
    """Record distress evidence and request a human intervention.

    This tool is for intervention, not routine logging. The retained raw is not
    sent through knowledge extraction.

    Args:
        summary: Short description of the problem.
        severity: One of blocked, degraded, or urgent.
        details: Optional context that may help the responder.
        requested_intervention: Optional action requested from the responder.
    """
    reporter = _reporter_extra(current_principal())
    result, message, level = await asyncio.to_thread(
        _report_distress_sync,
        summary,
        severity,
        details,
        requested_intervention,
        reporter,
    )
    if message is None or level is None:
        return result
    raw_id = result["intervention_id"]
    try:
        await _notify(message, level)
    except Exception:
        logger.exception("knowledge mcp: distress notification failed for %s", raw_id)
        return {"intervention_id": raw_id, "status": "recorded"}
    return {"intervention_id": raw_id, "status": "notified"}


@_knowledge_tool
async def list_tasks(
    status: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    size: str | None = None,
    include_someday: bool = False,
) -> dict:
    """List tasks with optional filters.

    Returns tasks sorted by most recently indexed. Someday tasks are
    excluded by default.

    Args:
        status: Comma-separated status filter (e.g. "todo,in-progress").
        due_before: ISO date — only tasks due on or before this date.
        due_after: ISO date — only tasks due on or after this date.
        size: Comma-separated size filter (e.g. "small,medium").
        include_someday: Include tasks with status "someday" (default false).
    """
    with Session(get_engine()) as session:
        tasks = KnowledgeStore(session).list_tasks(
            statuses=status.split(",") if status else None,
            due_before=due_before,
            due_after=due_after,
            sizes=size.split(",") if size else None,
            include_someday=include_someday,
        )
    return {"tasks": tasks}


@_knowledge_tool
async def search_tasks(
    query: str,
    status: str | None = None,
    include_someday: bool = False,
    limit: int = 20,
) -> dict:
    """Semantic search over tasks.

    Embeds the query and searches task notes by cosine similarity.

    Args:
        query: Natural language search query (minimum 2 characters).
        status: Comma-separated status filter (e.g. "todo,in-progress").
        include_someday: Include tasks with status "someday" (default false).
        limit: Maximum results to return (default 20).
    """
    if len(query) < 2:
        return {"tasks": []}

    embed_client = EmbeddingClient()
    try:
        vector = await embed_client.embed(query)
    except Exception:
        logger.exception("tasks mcp: embedding call failed")
        return {"error": "embedding unavailable"}

    with Session(get_engine()) as session:
        tasks = KnowledgeStore(session).search_tasks(
            query_embedding=vector,
            statuses=status.split(",") if status else None,
            include_someday=include_someday,
            limit=limit,
        )
    return {"tasks": tasks}


@_knowledge_tool
async def update_task(
    note_id: str,
    fields: dict,
) -> dict:
    """Update fields on a task.

    Merges the provided fields into the task's metadata. Automatically
    sets ``task-completed`` date when status transitions to done/cancelled,
    and clears it when moving away from those statuses.

    Args:
        note_id: The stable task identifier.
        fields: Dictionary of fields to update (e.g. {"status": "done"}).
    """
    with Session(get_engine()) as session:
        store = KnowledgeStore(session)
        try:
            store.patch_task(note_id, fields)
        except ValueError as exc:
            return {"error": str(exc)}
    return {"updated": True, "note_id": note_id}


@_knowledge_tool
async def get_daily_tasks() -> dict:
    """Get tasks due today or overdue.

    Returns tasks with a due date on or before today, excluding
    someday tasks.
    """
    with Session(get_engine()) as session:
        tasks = KnowledgeStore(session).list_tasks_daily()
    return {"tasks": tasks}


@_knowledge_tool
async def get_weekly_tasks() -> dict:
    """Get tasks due this week.

    Returns tasks with a due date between now and the end of the
    current week (Sunday), excluding someday tasks.
    """
    with Session(get_engine()) as session:
        tasks = KnowledgeStore(session).list_tasks_weekly()
    return {"tasks": tasks}


def register_mcp_tools(mcp_server: Any) -> None:
    """Register the full knowledge catalogue on a supplied MCP server."""
    for tool in _KNOWLEDGE_TOOLS:
        mcp_server.add_tool(tool)


# ---------------------------------------------------------------------------
# Gap lifecycle tools
# ---------------------------------------------------------------------------
# reconciler permanently defers without an Obsidian sidecar.
# ---------------------------------------------------------------------------

_ATOM_TYPES = frozenset({"atom", "fact", "active"})
_VISIBILITIES = frozenset({"public", "private"})
_ACTIVE_STATUSES = frozenset({"active", "someday", "blocked"})
_ACTIVE_SIZES = frozenset({"small", "medium", "large", "unknown"})
