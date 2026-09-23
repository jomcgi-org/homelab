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

from sqlmodel import Session, select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_decisions import _iso, _request_records
from factory.orchestration.factory_models import FactoryReceipt

_REPORT_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="factory-knowledge")
_REPORT_SLOTS = BoundedSemaphore(2)
_PLANNER_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="planner-context")
_PLANNER_SLOTS = BoundedSemaphore(2)
PLANNER_KNOWLEDGE_LIMIT = 5
PLANNER_KNOWLEDGE_CANDIDATE_LIMIT = 50
PLANNER_CONTEXT_FOLLOWUP_LIMIT = 1


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
            },
        }


def _receipt_raw_ids(
    db: Session, rows: list[dict], receipt_id: int, generation: int
) -> set[str]:
    """Raw provenance explicitly authorized for one exact receipt.

    Repository scope is only the search boundary. It does not authorize a
    different receipt, an interactive session, or an unscoped agent report.
    Older receipt reports predate the redundant generation field, so the
    durable receipt id is authoritative and a present generation must agree.
    """
    from knowledge.api import raw_extras_by_id

    raw_ids = {
        item.get("raw_id")
        for row in rows
        for item in row.get("provenance", [])
        if isinstance(item.get("raw_id"), str)
    }
    if not raw_ids:
        return set()
    raw_extras = raw_extras_by_id(db, list(raw_ids))
    authorized = set()
    for raw_id, extra in raw_extras.items():
        recorded_receipt = extra.get("factory_receipt_id")
        recorded_generation = extra.get("factory_receipt_generation")
        if type(recorded_receipt) is not int or recorded_receipt != receipt_id:
            continue
        if recorded_generation is not None and (
            type(recorded_generation) is not int or recorded_generation != generation
        ):
            continue
        authorized.add(raw_id)
    return authorized


async def retrieve_knowledge(
    query: str,
    scope: str,
    limit: int,
    *,
    receipt_id: int | None = None,
    generation: int | None = None,
) -> dict:
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
                rows = KnowledgeStore(db).search_notes_with_context(
                    vector,
                    limit=(
                        limit
                        if receipt_id is None
                        else min(
                            PLANNER_KNOWLEDGE_CANDIDATE_LIMIT,
                            max(limit, limit * 10),
                        )
                    ),
                    scope_filter=scope,
                    # The operator context keeps its existing current-only
                    # behavior. A planner must see an authorized invalidation
                    # as evidence rather than silently interpreting it as an
                    # empty successful search.
                    exclude_invalidated=receipt_id is None,
                )
                authorized = (
                    None
                    if receipt_id is None
                    else _receipt_raw_ids(db, rows, receipt_id, generation)
                )
                return rows, authorized

        rows, authorized_raw_ids = await asyncio.wait_for(
            asyncio.to_thread(search), timeout=4
        )
    except Exception:
        retrieved_at = _now()
        return {
            "status": "unavailable",
            "scope": scope,
            "observed_at": retrieved_at,
            "retrieved_at": retrieved_at,
            "notes": [],
            "omitted": {
                "unauthorized_candidates": 0,
                "result_limit": 0,
                "prompt_budget": 0,
            },
        }
    # Do not expand graph neighbours: an edge can point outside this scope.
    notes = []
    unauthorized = 0
    result_limit = 0
    for row in rows:
        if row.get("scope") != scope:
            unauthorized += 1
            continue
        candidate_raw_ids = [
            item.get("raw_id")
            for item in row.get("provenance", [])
            if isinstance(item.get("raw_id"), str)
        ]
        if authorized_raw_ids is not None and (
            not candidate_raw_ids
            or any(raw_id not in authorized_raw_ids for raw_id in candidate_raw_ids)
        ):
            unauthorized += 1
            continue
        raw_ids = candidate_raw_ids
        if len(notes) >= limit:
            result_limit += 1
            continue
        notes.append(
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
            | {"evidence_raw_ids": raw_ids}
        )
    retrieved_at = _now()
    return {
        "status": "available",
        "scope": scope,
        "observed_at": retrieved_at,
        "retrieved_at": retrieved_at,
        "authority": (
            "untrusted_context_only"
            if receipt_id is None
            else "receipt_authorized_untrusted_evidence"
        ),
        "notes": notes,
        "omitted": {
            "unauthorized_candidates": unauthorized,
            "result_limit": result_limit,
            "prompt_budget": 0,
        },
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


def _planner_factory_context(task_id: str) -> dict:
    """Current authoritative records for one server-selected planner task."""
    from factory.orchestration.factory_models import FactoryAudit
    from factory.orchestration.models import SwarmTask

    with controls._read_session() as db:
        task = db.get(SwarmTask, task_id)
        receipts = db.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task_id)
        ).all()
        if task is None or len(receipts) != 1:
            return {"ok": False, "reason": "invalid_receipt_authorization"}
        row = receipts[0]
        try:
            pinned_policy = json.loads(row.policy_json or "")
        except (TypeError, ValueError):
            pinned_policy = None
        if (
            row.id is None
            or type(row.generation) is not int
            or task.repo != row.repo
            or task.workflow_id != f"factory:{task_id}"
            or not isinstance(pinned_policy, dict)
            or pinned_policy.get("repo") != row.repo
            or pinned_policy.get("generation") != row.generation
        ):
            return {"ok": False, "reason": "invalid_receipt_authorization"}
        snapshot = controls._snapshot(db, row, body=True)
        exchanges = [
            {
                "actor": actor,
                "request": {
                    key: item["request"].get(key)
                    for key in ("request_key", "decision_id", "action", "option_key")
                },
                "outcome": {
                    key: item["result"].get(key)
                    for key in ("state", "acknowledged_at", "reason", "blocked_by")
                },
            }
            for (actor, _key), item in _request_records(db).items()
            if item["request"]["receipt_id"] == row.id
        ][-10:]
        followups = []
        result_rows = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action == "planner_context_result",
            )
            .order_by(FactoryAudit.id.desc())
            .limit(PLANNER_CONTEXT_FOLLOWUP_LIMIT)
        ).all()
        for result_row in reversed(result_rows):
            detail = json.loads(result_row.detail_json)
            followups.append(
                {
                    key: detail.get(key)
                    for key in (
                        "request_id",
                        "request_audit_id",
                        "query",
                        "requested_at",
                        "completed_at",
                        "bounds",
                        "authorization",
                        "knowledge",
                        "error",
                    )
                    if detail.get(key) is not None
                }
            )
        return {
            "ok": True,
            "authorization": {
                "status": "authorized",
                "task_id": task_id,
                "receipt_id": row.id,
                "generation": row.generation,
                "repo": row.repo,
            },
            "observed_at": _now(),
            "receipt_updated_at": _iso(row.updated_at),
            "acceptance": {
                "source": "factory_receipt",
                "title": row.title,
                "body": row.body,
                "url": row.url,
            },
            "operator_constraints": snapshot.get("policy") or {},
            "operator_direction": snapshot.get("direction"),
            "control": {
                key: snapshot.get(key)
                for key in (
                    "state",
                    "task_paused",
                    "cancellation_requested",
                    "routing_tier",
                    "allowance",
                    "limits",
                    "deadline_at",
                    "turns_used",
                    "planner_turns_used",
                    "committed_cost_usd",
                    "unresolved_starts",
                )
            },
            "decision": snapshot.get("escalation"),
            "recent_exchanges": exchanges,
            "followups": followups,
        }


async def planner_context(
    task_id: str, query: str | None = None, limit: int = PLANNER_KNOWLEDGE_LIMIT
) -> dict:
    """Receipt-authorized current evidence for one planner invocation."""
    current = await asyncio.to_thread(_planner_factory_context, task_id)
    if not current.get("ok"):
        return current
    authorization = current["authorization"]
    acceptance = current["acceptance"]
    search_query = (
        query
        or "\n".join(
            (str(acceptance.get("title") or ""), str(acceptance.get("body") or ""))
        )[:2000]
    )
    current["knowledge"] = await retrieve_knowledge(
        search_query,
        "repo:" + authorization["repo"],
        min(max(1, limit), PLANNER_KNOWLEDGE_LIMIT),
        receipt_id=authorization["receipt_id"],
        generation=authorization["generation"],
    )
    return current


def planner_context_with_deadline(task_id: str, query: str, timeout: int) -> dict:
    """Bound the complete context read, including its synchronous DB work."""
    if not _PLANNER_SLOTS.acquire(blocking=False):
        return {"ok": False, "reason": "context_retrieval_busy"}

    def load():
        try:
            return asyncio.run(planner_context(task_id, query=query))
        finally:
            _PLANNER_SLOTS.release()

    future = _PLANNER_POOL.submit(load)
    try:
        return future.result(timeout=timeout)
    except FutureTimeout:
        return {"ok": False, "reason": "context_timeout"}
    except Exception:
        return {"ok": False, "reason": "context_retrieval_failed"}


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
            "not a model suggestion, is the evidence. Factory records remain "
            "authoritative "
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
                "factory_receipt_generation": row.generation,
                "factory_decision_id": item["request"]["decision_id"],
            },
        )
        return {
            "status": "queued" if created else "duplicate",
            "raw_id": raw.raw_id,
            "scope": scope,
            "verification_state": "unverified",
        }
