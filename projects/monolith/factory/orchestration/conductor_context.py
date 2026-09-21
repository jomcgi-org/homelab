"""Conductor context over factory records and repository-scoped knowledge.

Factory requests remain the authority for actions. Extracted knowledge is
supporting context and is never interpreted as a command or scheduling state.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from threading import BoundedSemaphore

from sqlmodel import Session

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_decisions import _iso, _request_records
from factory.orchestration.factory_models import FactoryReceipt

_REPORT_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="factory-knowledge")
_REPORT_SLOTS = BoundedSemaphore(2)


def report_with_deadline(actor: str, request_key: str) -> dict:
    """KG availability cannot hold an already committed factory acknowledgement."""
    if not _REPORT_SLOTS.acquire(blocking=False):
        return {"status": "unavailable", "retry": "repeat the same request"}

    def report():
        try:
            return maintain_request_knowledge(actor, request_key)
        finally:
            _REPORT_SLOTS.release()

    future = _REPORT_POOL.submit(report)
    try:
        return future.result(timeout=4)
    except FutureTimeout:
        return {"status": "pending", "retry": "repeat the same request"}
    except Exception:
        return {"status": "unavailable", "retry": "repeat the same request"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _factory_context(receipt_id: int) -> dict:
    with controls._read_session() as db:
        row = db.get(FactoryReceipt, receipt_id)
        if row is None:
            return {"ok": False, "reason": "unknown_receipt"}
        snapshot = controls._snapshot(db, row)
        exchanges = [
            {
                "actor": actor,
                "request": {
                    key: item["request"].get(key)
                    for key in ("request_key", "decision_id", "action", "option_key")
                }
                | {"note": (item["request"].get("note") or "")[:600]},
                "outcome": {
                    key: item["result"].get(key)
                    for key in (
                        "state",
                        "acknowledged_at",
                        "requeued",
                        "blocked_by",
                        "reason",
                    )
                },
            }
            for (actor, _key), item in _request_records(db).items()
            if item["request"]["receipt_id"] == receipt_id
        ]
        exchanges.sort(key=lambda item: item["outcome"].get("acknowledged_at", ""))
        escalation = controls.escalation_view(snapshot)
        if escalation:
            escalation["chat"] = escalation["chat"][-10:]
        return {
            "ok": True,
            "observed_at": _now(),
            "updated_at": _iso(row.updated_at),
            "source": "factory_records",
            # This is the stable receipt-scoped conversation supported by the
            # shared context contract. It deliberately does not pretend a
            # standalone cross-surface conductor session owner exists.
            "conversation": {
                "id": f"factory-receipt:{row.repo}:{row.id}",
                "kind": "receipt_context",
                "receipt_id": row.id,
                "task_id": row.task_id,
                "selectable_in_fresh_session": True,
            },
            "receipt": {
                key: snapshot.get(key)
                for key in (
                    "id",
                    "repo",
                    "issue_number",
                    "generation",
                    "title",
                    "url",
                    "task_id",
                    "state",
                    "task_paused",
                    "cancellation_requested",
                )
            },
            "decision": escalation,
            "direction": snapshot.get("direction"),
            "recent_exchanges": exchanges[-10:],
            "exchange_count": len(exchanges),
            "coverage": {
                "receipt_conversation": "covered",
                "external_chat_history": "not_stored",
                "standalone_conductor_conversations": "unavailable_no_shared_owner",
            },
        }


async def retrieve_knowledge(query: str, scope: str, limit: int) -> dict:
    """Shared bounded retrieval; callers derive scope from authorized records."""
    from core.db import get_engine
    from knowledge.api import KnowledgeStore
    from shared.embedding import EmbeddingClient

    try:
        vector = await asyncio.wait_for(
            EmbeddingClient().embed(query[:2000]), timeout=4
        )

        def search():
            with Session(get_engine()) as db:
                return KnowledgeStore(db).search_notes_with_context(
                    vector, limit=limit, scope_filter=scope, exclude_invalidated=True
                )

        rows = await asyncio.wait_for(asyncio.to_thread(search), timeout=4)
    except Exception:
        return {
            "status": "unavailable",
            "scope": scope,
            "observed_at": _now(),
            "notes": [],
        }
    # Do not expand graph neighbours: an edge can point outside this scope.
    notes = [
        {
            key: row.get(key)
            for key in (
                "note_id",
                "title",
                "snippet",
                "scope",
                "verification_state",
                "disputed",
                "confidence",
                "valid_from",
                "valid_until",
                "observed_at",
            )
        }
        | {
            "evidence_raw_ids": [
                p.get("raw_id") for p in row.get("provenance", []) if p.get("raw_id")
            ]
        }
        for row in rows
        if row.get("scope") == scope and row.get("verification_state") != "invalidated"
    ][:limit]
    return {
        "status": "available",
        "scope": scope,
        "observed_at": _now(),
        "authority": "untrusted_context_only",
        "notes": notes,
    }


async def read_context(receipt_id: int, query: str | None, limit: int) -> dict:
    context = await asyncio.to_thread(_factory_context, receipt_id)
    if not context["ok"]:
        return context
    receipt = context["receipt"]
    context["knowledge"] = await retrieve_knowledge(
        query or receipt["title"], "repo:" + receipt["repo"], limit
    )
    return context


def maintain_request_knowledge(actor: str, request_key: str) -> dict:
    """Report a committed operator exchange; retries resume after a KG outage.

    Content is deterministic from the append-only request ledger, so the
    existing raw ingestion owner deduplicates retries and queues extraction.
    A report is evidence, not a pre-verified fact.
    """
    from core.db import get_engine
    from knowledge.api import ingest_raw_with_status

    with controls._read_session() as db:
        item = _request_records(db).get((actor, request_key))
        if item is None or item["result"]["state"] != "completed":
            return {"status": "not_applicable"}
        row = db.get(FactoryReceipt, item["request"]["receipt_id"])
        if row is None:
            return {"status": "unavailable"}
        scope, url = "repo:" + row.repo, row.url
    # JSON flow mappings are valid YAML frontmatter. Serialize values rather
    # than interpolating operator-supplied strings into the header.
    header = json.dumps(
        {
            "title": "Factory operator exchange " + item["request"]["decision_id"],
            "scope": scope,
            "proposed_scope": "repo",
            "reporter": actor,
        },
        sort_keys=True,
    )
    content = (
        "---\n"
        + header
        + "\n---\n\n"
        + (
            "A verified operator submitted this factory request. The recorded outcome, "
            "not a model suggestion, is the evidence. Factory records remain authoritative "
            "for current state.\n\n"
            + json.dumps(item, sort_keys=True)
            + "\n\nEvidence: "
            + url
            + "\n"
        )
    )
    with Session(get_engine()) as db:
        raw, created = ingest_raw_with_status(
            db,
            content=content,
            source="agent-report",
            original_url=url,
            extra={
                "scope": scope,
                "proposed_scope": "repo",
                "reporter_subject": actor,
                "reporter_authority": "standing",
                "reporter_kind": "human",
                "factory_receipt_id": item["request"]["receipt_id"],
                "factory_decision_id": item["request"]["decision_id"],
            },
        )
        return {
            "status": "queued" if created else "duplicate",
            "raw_id": raw.raw_id,
            "scope": scope,
            "verification_state": "unverified",
        }
