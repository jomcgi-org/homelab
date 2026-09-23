"""Conductor context over factory records and repository-scoped knowledge.

Factory requests remain the authority for actions. Extracted knowledge is
supporting context and is never interpreted as a command or scheduling state.
"""

from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from threading import BoundedSemaphore

from sqlmodel import Session, select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_decisions import _iso, _request_records
from factory.orchestration.factory_models import FactoryControl, FactoryReceipt

_REPORT_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="factory-knowledge")
_REPORT_SLOTS = BoundedSemaphore(2)
_PLANNER_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="planner-context")
_PLANNER_SLOTS = BoundedSemaphore(2)
PLANNER_KNOWLEDGE_LIMIT = 5
PLANNER_KNOWLEDGE_CANDIDATE_LIMIT = 50
PLANNER_CONTEXT_FOLLOWUP_LIMIT = 1
RECENT_EXCHANGE_LIMIT = 10


def continuity_enabled() -> bool:
    """Whether receipt continuity maintenance and retrieval are staged on."""
    return os.environ.get("CONDUCTOR_CONTINUITY_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def report_with_deadline(actor: str, request_key: str) -> dict:
    """KG availability cannot hold an already committed factory acknowledgement."""
    if not continuity_enabled():
        return {"status": "disabled"}
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


def with_request_knowledge(result: dict, actor: str, request_key: str) -> dict:
    """Maintain one completed durable request for every transport surface."""
    if result.get("state") != "completed":
        return result
    return {**result, "knowledge": report_with_deadline(actor, request_key)}


def report_receipt_with_deadline(receipt_id: int) -> dict:
    """Queue a new Conductor brief without holding its factory settlement."""
    if not continuity_enabled():
        return {"status": "disabled"}
    if not _REPORT_SLOTS.acquire(blocking=False):
        return {"status": "unavailable", "retry": "next reconciliation"}

    def report():
        try:
            return maintain_receipt_knowledge(receipt_id)
        finally:
            _REPORT_SLOTS.release()

    future = _REPORT_POOL.submit(report)
    try:
        return future.result(timeout=4)
    except FutureTimeout:
        return {"status": "pending", "retry": "next reconciliation"}
    except Exception:
        return {"status": "unavailable", "retry": "next reconciliation"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_stale(row: dict) -> bool:
    if row.get("verification_state") == "invalidated":
        return True
    valid_until = row.get("valid_until")
    if not isinstance(valid_until, str):
        return False
    try:
        end = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
    except ValueError:
        return True
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end <= datetime.now(timezone.utc)


def _request_record(actor: str, item: dict, row: FactoryReceipt) -> dict:
    """Project one decision request without calling it a conversation turn."""
    request = item["request"]
    result = item["result"]
    action = request.get("action")
    return {
        "actor": actor,
        "source": "factory_decision",
        "timestamp": result.get("acknowledged_at"),
        "conversation": f"factory-receipt:{row.id}",
        "kind": "operator_decision" if action == "decide" else "operator_question",
        "epistemic_status": (
            "approved"
            if action == "decide" and result.get("state") == "completed"
            else "operator_input"
        ),
        "references": {
            "receipt_id": row.id,
            "repo": row.repo,
            "issue_number": row.issue_number,
            "task_id": row.task_id,
            "decision_id": request.get("decision_id"),
            "request_key": request.get("request_key"),
        },
        "request": {
            key: request.get(key)
            for key in ("request_key", "decision_id", "action", "option_key")
        }
        | {"note": (request.get("note") or "")[:600]},
        "outcome": {
            key: result.get(key)
            for key in (
                "state",
                "acknowledged_at",
                "requeued",
                "blocked_by",
                "reason",
            )
        },
    }


def _recent_conversation(
    row: FactoryReceipt, actor: str
) -> tuple[list[dict], list[dict]]:
    """Project only producer-authored exchanges for this receipt and actor."""
    escalation = json.loads(row.escalation_json) if row.escalation_json else {}
    exchanges: list[dict] = []
    summaries: list[dict] = []
    for item in escalation.get("conversation") or []:
        if not isinstance(item, dict) or item.get("role") not in (
            "operator",
            "conductor",
        ):
            continue
        if item["role"] == "operator" and item.get("actor") != actor:
            continue
        if (
            item["role"] == "conductor"
            and item.get("audience_actor") is not None
            and item.get("audience_actor") != actor
        ):
            continue
        message_id = str(item.get("message_id") or "")
        if not message_id:
            continue
        exchanges.append(
            {
                "message_id": message_id,
                "actor": item.get("actor"),
                "role": item["role"],
                "source": item.get("source"),
                "timestamp": item.get("timestamp"),
                "conversation": f"factory-receipt:{row.id}",
                "epistemic_status": item.get("epistemic_status"),
                "text": str(item.get("text") or "")[:1200],
                "references": {
                    "receipt_id": row.id,
                    "repo": row.repo,
                    "issue_number": row.issue_number,
                    "task_id": item.get("task_id"),
                    "decision_id": item.get("decision_id"),
                    "request_key": item.get("request_key"),
                },
            }
        )
        summary = str(item.get("summary") or "").strip()
        if summary:
            summaries.append(
                {
                    "text": summary[:1200],
                    "evidence": [message_id],
                    "receipt_id": row.id,
                    "task_id": item.get("task_id"),
                }
            )
    return exchanges[-RECENT_EXCHANGE_LIMIT:], summaries[-RECENT_EXCHANGE_LIMIT:]


def _current_records(
    db: Session, row: FactoryReceipt, *, actor: str
) -> tuple[dict, dict, dict | None]:
    """Read current control, queue, task and decision records together."""
    snapshot = controls._snapshot(db, row)
    control = db.get(FactoryControl, "factory")
    queue = db.exec(
        select(FactoryReceipt)
        .where(
            FactoryReceipt.repo == row.repo,
            FactoryReceipt.state == "queued",
        )
        .order_by(FactoryReceipt.created_at, FactoryReceipt.id)
    ).all()
    queued_ids = [item.id for item in queue]
    position = queued_ids.index(row.id) + 1 if row.id in queued_ids else None
    policy = snapshot.get("policy") or {}
    control_policy = json.loads(control.policy_json or "{}") if control else {}
    task = {
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
            "task_class",
            "routing_tier",
            "admitted_at",
            "deadline_at",
            "turns_used",
            "planner_turns_used",
            "committed_cost_usd",
            "limits",
            "allowance",
        )
    }
    task["budgets"] = {
        key: policy.get(key)
        for key in (
            "task_budget_usd",
            "turn_budget_usd",
            "max_task_turns_hard",
            "max_planner_turns",
        )
    }
    current_control = (
        {
            "state": control.state,
            "version": control.version,
            "actor": control.actor,
            "updated_at": _iso(control.updated_at),
            "stopped_at": _iso(control.stopped_at) if control.stopped_at else None,
            "policy_generation": control_policy.get("generation"),
            "auto_merge": control_policy.get("auto_merge", False) is True,
        }
        if control is not None
        else {"state": "disabled", "version": None}
    )
    current_queue = {
        "source": "factory_receipt",
        "order": "created_at_then_id",
        "queued_count": len(queued_ids),
        "position": position,
        "ahead": max(0, position - 1) if position is not None else None,
    }
    if snapshot.get("escalation"):
        chat = snapshot["escalation"].get("chat") or []
        snapshot["escalation"]["chat"] = [
            entry for entry in chat if entry.get("actor") == actor
        ]
    escalation = controls.escalation_view(snapshot)
    if escalation:
        escalation["recommendation_status"] = "conductor_suggestion"
        escalation["resolution_status"] = (
            "operator_decision" if escalation.get("resolved") is not None else None
        )
        escalation["chat"] = escalation["chat"][-RECENT_EXCHANGE_LIMIT:]
    return (
        {"control": current_control, "queue": current_queue, "task": task},
        snapshot,
        escalation,
    )


def _factory_context(receipt_id: int, *, actor: str) -> dict:
    with controls._read_session() as db:
        row = db.get(FactoryReceipt, receipt_id)
        if row is None:
            return {"ok": False, "reason": "unknown_receipt"}
        if not actor:
            return {"ok": False, "reason": "operator_identity_required"}
        current, snapshot, escalation = _current_records(db, row, actor=actor)
        requests = [
            _request_record(request_actor, item, row)
            for (request_actor, _key), item in _request_records(db).items()
            if item["request"]["receipt_id"] == receipt_id and request_actor == actor
        ]
        requests.sort(key=lambda item: item.get("timestamp") or "")
        continuity = continuity_enabled()
        exchanges, summaries = (
            _recent_conversation(row, actor) if continuity else ([], [])
        )
        return {
            "ok": True,
            "observed_at": _now(),
            "updated_at": _iso(row.updated_at),
            "source": "factory_records",
            "audience": "conductor",
            "authority": "current_factory_records_only",
            "scope": {
                "operator": actor,
                "repo": row.repo,
                "task_id": row.task_id,
                "receipt_id": row.id,
            },
            "current": current,
            "receipt": current["task"],
            "decision": escalation,
            "direction": snapshot.get("direction"),
            "recent_exchanges": exchanges,
            "exchange_count": len(exchanges),
            "recent_requests": requests[-RECENT_EXCHANGE_LIMIT:],
            "request_count": len(requests),
            "summaries": summaries,
            "coverage": {
                "durable_factory_conversation": (
                    "covered" if continuity else "staged_off"
                ),
                "decision_requests": "covered",
                "generic_agent_turns": "excluded_without_conductor_identity",
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
    scopes: str | tuple[str, ...],
    limit: int,
    *,
    receipt_id: int | None = None,
    generation: int | None = None,
) -> dict:
    """Shared bounded retrieval; callers derive scopes from authorized records."""
    from core.db import get_engine
    from knowledge.api import KnowledgeStore
    from shared.embedding import EmbeddingClient

    if (receipt_id is None) != (generation is None):
        raise ValueError("receipt_id and generation must be provided together")
    requested_scopes = (scopes,) if isinstance(scopes, str) else scopes
    if not requested_scopes:
        raise ValueError("at least one authorized scope is required")

    try:
        vector = await asyncio.wait_for(
            EmbeddingClient().embed(query[:2000]), timeout=4
        )

        def search():
            with Session(get_engine()) as db:
                store = KnowledgeStore(db)
                candidate_limit = (
                    limit
                    if receipt_id is None
                    else min(
                        PLANNER_KNOWLEDGE_CANDIDATE_LIMIT,
                        max(limit, limit * 10),
                    )
                )
                rows = [
                    row
                    for scope in requested_scopes
                    for row in store.search_notes_with_context(
                        vector,
                        limit=candidate_limit,
                        scope_filter=scope,
                        # Corrections stay visible with invalidation and
                        # provenance rather than becoming a successful empty
                        # search for either authorized audience.
                        exclude_invalidated=False,
                    )
                ]
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
            "scope": scopes if isinstance(scopes, str) else None,
            "scopes": list(requested_scopes),
            "observed_at": retrieved_at,
            "retrieved_at": retrieved_at,
            "notes": [],
            "omitted": {
                "unauthorized_candidates": 0,
                "result_limit": 0,
                "prompt_budget": 0,
            },
        }
    # Do not expand graph neighbours: an edge can point outside these scopes.
    authorized_scopes = set(requested_scopes)
    ordered_rows = sorted(
        rows,
        key=lambda row: float(row.get("score") or 0.0),
        reverse=True,
    )
    unique_rows = list(
        {
            row.get("note_id"): row
            for row in ordered_rows
            if row.get("note_id") is not None
        }.values()
    )
    scoped_ids = {
        row.get("note_id")
        for row in unique_rows
        if row.get("scope") in authorized_scopes
    }
    notes = []
    unauthorized = 0
    result_limit = 0
    for row in unique_rows:
        if row.get("scope") not in authorized_scopes:
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
                    "score",
                )
            }
            | {
                "stale": _is_stale(row),
                "evidence_raw_ids": candidate_raw_ids,
                "supersedes": [
                    edge.get("target_id")
                    for edge in row.get("edges", [])
                    if edge.get("edge_type") == "supersedes"
                    and edge.get("target_id") in scoped_ids
                ],
            }
        )
    retrieved_at = _now()
    return {
        "status": "available",
        "scope": scopes if isinstance(scopes, str) else None,
        "scopes": list(requested_scopes),
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


async def read_context(
    receipt_id: int,
    query: str | None,
    limit: int,
    *,
    actor: str,
) -> dict:
    context = await asyncio.to_thread(_factory_context, receipt_id, actor=actor)
    if not context["ok"]:
        return context
    receipt = context["receipt"]
    scopes: str | tuple[str, ...] = "repo:" + receipt["repo"]
    if continuity_enabled():
        scopes = (
            f"personal:{actor}:factory-receipt:{receipt_id}",
            f"session:factory-receipt:{receipt_id}",
        )
    context["knowledge"] = await retrieve_knowledge(
        query or receipt["title"], scopes, limit
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
        decision = controls.escalation_view(snapshot)
        if decision is not None:
            # Operator chat is actor-private continuity input. The Planner gets
            # the pending question and settled resolution, not private replies.
            decision["chat"] = []
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
            "decision": decision,
            "recent_exchanges": [],
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
        f"session:factory-receipt:{authorization['receipt_id']}",
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

    if not continuity_enabled():
        return {"status": "disabled"}
    with controls._read_session() as db:
        item = _request_records(db).get((actor, request_key))
        if item is None or item["result"]["state"] != "completed":
            return {"status": "not_applicable"}
        row = db.get(FactoryReceipt, item["request"]["receipt_id"])
        if row is None:
            return {"status": "unavailable"}
        scope = f"personal:{actor}:factory-receipt:{row.id}"
        url = row.url
        generation = row.generation
        escalation = json.loads(row.escalation_json) if row.escalation_json else {}
        evidence_message_ids = [
            message.get("message_id")
            for message in escalation.get("conversation") or []
            if message.get("message_id")
            and message.get("decision_id") == item["request"]["decision_id"]
            and (
                (
                    message.get("role") == "conductor"
                    and message.get("audience_actor") in (None, actor)
                )
                or (message.get("role") == "operator" and message.get("actor") == actor)
            )
        ]
    # Transport idempotency keys and replay status are not claim evidence. A
    # later transport retry that retells the same decision must hash to the
    # same raw input and cannot strengthen it through repetition.
    evidence_item = {
        "request": {
            key: item["request"].get(key)
            for key in (
                "receipt_id",
                "decision_id",
                "option_key",
                "note",
                "action",
            )
        },
        "result": {
            key: item["result"].get(key)
            for key in (
                "state",
                "receipt_id",
                "decision_id",
                "resolution",
                "requeued",
                "blocked_by",
            )
        },
        "evidence_message_ids": evidence_message_ids,
    }
    # JSON flow mappings are valid YAML frontmatter. Serialize values rather
    # than interpolating operator-supplied strings into the header.
    header = json.dumps(
        {
            "title": "Factory operator exchange " + item["request"]["decision_id"],
            "scope": scope,
            "proposed_scope": "personal",
            "reporter": actor,
        },
        sort_keys=True,
    )
    content = (
        "---\n"
        + header
        + "\n---\n\n"
        + (
            "An authenticated operator submitted this factory request. The recorded "
            "outcome is operator evidence, not a model suggestion. Retelling does not "
            "increase certainty, and extraction keeps the claim unverified until its "
            "verification owner changes that state. Factory records remain "
            "authoritative for current state. Later corrections should supersede "
            "older extracted knowledge while retaining both raw evidence links.\n\n"
            + json.dumps(evidence_item, sort_keys=True)
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
                "proposed_scope": "personal",
                "reporter_subject": actor,
                "reporter_authority": "standing",
                "reporter_kind": "human",
                "factory_receipt_id": item["request"]["receipt_id"],
                "factory_receipt_generation": generation,
                "factory_decision_id": item["request"]["decision_id"],
            },
        )
        return {
            "status": "queued" if created else "duplicate",
            "raw_id": raw.raw_id,
            "scope": scope,
            "verification_state": "unverified",
        }


def maintain_receipt_knowledge(receipt_id: int) -> dict:
    """Report public and actor-private receipt exchanges into separate scopes."""
    from core.db import get_engine
    from knowledge.api import ingest_raw_with_status

    with controls._read_session() as db:
        row = db.get(FactoryReceipt, receipt_id)
        if row is None:
            return {"status": "unavailable"}
        escalation = json.loads(row.escalation_json) if row.escalation_json else {}
        conversation = [
            item
            for item in escalation.get("conversation") or []
            if isinstance(item, dict)
        ]
        public = [
            item
            for item in conversation
            if item.get("role") == "conductor" and item.get("audience_actor") is None
        ]
        actors = sorted(
            {
                str(item["actor"])
                for item in conversation
                if item.get("role") == "operator" and item.get("actor")
            }
            | {
                str(item["audience_actor"])
                for item in conversation
                if item.get("role") == "conductor" and item.get("audience_actor")
            }
        )
        scoped: list[tuple[str, dict]] = [
            (f"session:factory-receipt:{row.id}", message) for message in public
        ]
        for actor in actors:
            private = [
                item
                for item in conversation
                if (item.get("role") == "operator" and item.get("actor") == actor)
                or (
                    item.get("role") == "conductor"
                    and item.get("audience_actor") == actor
                )
            ]
            scoped.extend(
                (f"personal:{actor}:factory-receipt:{row.id}", message)
                for message in private
            )
        if not scoped:
            return {"status": "not_applicable"}
        url = row.url
        repo = row.repo
        issue_number = row.issue_number
        generation = row.generation
    reports = []
    with Session(get_engine()) as db:
        for scope, message in scoped:
            proposed_scope = "personal" if scope.startswith("personal:") else "session"
            header = json.dumps(
                {
                    "title": (
                        f"Factory receipt {receipt_id} message "
                        f"{message.get('message_id')}"
                    ),
                    "scope": scope,
                    "proposed_scope": proposed_scope,
                    "reporter": "factory:conductor",
                },
                sort_keys=True,
            )
            content = json.dumps(
                {
                    "receipt_id": receipt_id,
                    "repo": repo,
                    "issue_number": issue_number,
                    "conversation": [message],
                    "evidence_message_ids": [message.get("message_id")],
                },
                sort_keys=True,
            )
            body = (
                "---\n"
                + header
                + "\n---\n\n"
                + "Conductor briefs are suggestions or hypotheses; authenticated "
                + "operator replies retain their recorded epistemic status. Current "
                + "factory records remain authoritative. Summaries link to "
                + "evidence_message_ids.\n\n"
                + content
                + "\n\nEvidence: "
                + url
                + "\n"
            )
            raw, created = ingest_raw_with_status(
                db,
                content=body,
                source="agent-report",
                original_url=url,
                extra={
                    "scope": scope,
                    "proposed_scope": proposed_scope,
                    "reporter_subject": "factory:conductor",
                    "reporter_authority": "system",
                    "reporter_kind": "agent",
                    "factory_receipt_id": receipt_id,
                    "factory_receipt_generation": generation,
                },
            )
            reports.append(
                {
                    "status": "queued" if created else "duplicate",
                    "raw_id": raw.raw_id,
                    "scope": scope,
                }
            )
    return {
        "status": (
            "queued"
            if any(item["status"] == "queued" for item in reports)
            else "duplicate"
        ),
        "reports": reports,
        "scopes": sorted({item["scope"] for item in reports}),
        "verification_state": "unverified",
    }
