"""Bounded, SELECT-only permit metadata. Never infer permission to recover work."""

from datetime import datetime, timezone
import os
import re

from sqlalchemy import text
from sqlmodel import Session

from core.db import get_engine


def _rows(db, sql, params):
    if db.bind.dialect.name == "sqlite":
        for schema in ("agent_sessions.", "claude_agent.", "dbos.", "knowledge."):
            sql = sql.replace(schema, "")
    return [dict(row) for row in db.execute(text(sql), params).mappings()]


def _safe(value):
    if isinstance(value, datetime):
        return (
            value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        ).isoformat()
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    return value


def _dispatch_metadata(db, params):
    # Project only the three fields consumed by terminal_dispatch_cessation.
    # Never load the recovery document: it can contain partial private output.
    if db.bind.dialect.name == "sqlite":
        document = "CASE WHEN json_valid(usage_json) THEN usage_json ELSE '{}' END"
        fields = ", ".join(
            f"CASE WHEN json_type({document}, '$.recovery.{key}') = '{kind}' "
            f"THEN {('substr(' if kind == 'text' else '')}"
            f"json_extract({document}, '$.recovery.{key}')"
            f"{(', 1, 256)' if kind == 'text' else '')} END AS {key}"
            for key, kind in (
                ("claim_owner", "text"),
                ("dispatch_count", "integer"),
                ("last_dispatch_at", "text"),
            )
        )
    else:
        document = (
            "CASE WHEN pg_input_is_valid(usage_json, 'jsonb') "
            "THEN usage_json::jsonb ELSE '{}'::jsonb END"
        )
        fields = ", ".join(
            f"CASE WHEN jsonb_typeof(({document})->'recovery'->'{key}') = '{kind}' "
            f"THEN {('left(' if kind == 'string' else '')}"
            f"({document})->'recovery'{('->>' if kind == 'string' else '->')}'{key}'"
            f"{(', 256)' if kind == 'string' else '')} END AS {key}"
            for key, kind in (
                ("claim_owner", "string"),
                ("dispatch_count", "number"),
                ("last_dispatch_at", "string"),
            )
        )
    rows = _rows(
        db,
        f"SELECT {fields} FROM agent_sessions.agent_turns "
        "WHERE session_id=:id AND seq=:seq",
        params,
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "claim_owner": row["claim_owner"]
        if isinstance(row["claim_owner"], str)
        else None,
        "dispatch_count": row["dispatch_count"]
        if type(row["dispatch_count"]) is int
        else None,
        "last_dispatch_at": row["last_dispatch_at"]
        if isinstance(row["last_dispatch_at"], str)
        else None,
    }


def inspect_permits(permit_ids, *, session=None):
    if (
        not isinstance(permit_ids, list)
        or not 1 <= len(permit_ids) <= 20
        or any(type(p) is not int or not 1 <= p <= 2**63 - 1 for p in permit_ids)
        or len(set(permit_ids)) != len(permit_ids)
    ):
        raise ValueError("Supply one to twenty distinct positive permit IDs")
    if session is None:
        with Session(get_engine(), autoflush=False) as db:
            return inspect_permits(permit_ids, session=db)
    db = session
    items = []
    # Explicit projections keep payloads, credentials and free-form evidence
    # out of both the query result and the MCP response.
    for permit_id in permit_ids:
        params = {"id": permit_id}
        permits = _rows(
            db,
            """
            SELECT id, session_id, pending_seq, tier, state, outcome, owner,
                   routine_job_name, created_at, settled_at
            FROM agent_sessions.capacity_reservations WHERE id = :id
        """,
            params,
        )
        if not permits:
            items.append({"permit_id": permit_id, "found": False})
            continue
        permit = permits[0]
        item = {"permit_id": permit_id, "found": True, "reservation": permit}
        item["observation"] = next(
            iter(
                _rows(
                    db,
                    """
            SELECT reason, checked_at, settled_at, guest_id, generation,
                   invoke_started_at, cp_updated_at
            FROM agent_sessions.probe_observations WHERE permit_id = :id
        """,
                    params,
                )
            ),
            None,
        )
        params = {"id": permit["session_id"], "seq": permit["pending_seq"]}
        agents = _rows(
            db,
            """
            SELECT id, status, admission_tier, workflow_id, ember_session_id,
                   last_turn_at,
                   result_receipt_fence_id IS NOT NULL AS result_receipt_fenced,
                   guest_cleanup_id IS NOT NULL AS cleanup_pending
            FROM agent_sessions.agent_sessions WHERE id = :id
        """,
            params,
        )
        agent = next(iter(agents), None)
        item["session"] = agent
        item["turn"] = next(
            iter(
                _rows(
                    db,
                    """
            SELECT seq, terminal_reason, stop_reason, created_at
            FROM agent_sessions.agent_turns WHERE session_id = :id AND seq = :seq
        """,
                    params,
                )
            ),
            None,
        )
        item["dispatch"] = _dispatch_metadata(db, params)
        receipts = _rows(
            db,
            """
            SELECT id, seq, dispatch_count, claim_owner, guest_id, created_at,
                   accept_until, retain_until, superseded_at, received_at,
                   response_observed_at, response_observer_released_at,
                   result_sha256 IS NOT NULL AS result_digest_present,
                   result_body IS NOT NULL AS result_body_present
            FROM agent_sessions.result_receipts
            WHERE session_id=:id AND seq=:seq
            ORDER BY created_at DESC, id DESC LIMIT 6
            """,
            params,
        )
        item["result_receipts"] = receipts[:5]
        item["result_receipts_truncated"] = len(receipts) > 5
        item["pending_count"] = _rows(
            db,
            """
            SELECT COUNT(*) AS count FROM agent_sessions.pending_messages
            WHERE session_id = :id
        """,
            params,
        )[0]["count"]
        item["pending"] = _rows(
            db,
            """
            SELECT seq, claimed_by_replica, claimed_at, dispatch_count, last_dispatch_at
            FROM agent_sessions.pending_messages WHERE session_id = :id
            ORDER BY seq LIMIT 5
        """,
            params,
        )
        item["pending_truncated"] = item["pending_count"] > len(item["pending"])
        item["workflow"] = None
        if agent and agent["workflow_id"]:
            item["workflow"] = next(
                iter(
                    _rows(
                        db,
                        """
                SELECT workflow_uuid, status FROM dbos.workflow_status
                WHERE workflow_uuid = :workflow
            """,
                        {"workflow": agent["workflow_id"]},
                    )
                ),
                None,
            )
        item["routine"] = None
        if permit["routine_job_name"]:
            item["routine"] = next(
                iter(
                    _rows(
                        db,
                        """
                SELECT name, routine_kind, last_status, next_run_at,
                       locked_by, locked_at, ttl_secs
                FROM claude_agent.routine_jobs WHERE name = :name
            """,
                        {"name": permit["routine_job_name"]},
                    )
                ),
                None,
            )
        items.append(item)
    return _safe(
        {
            "observed_at": datetime.now(timezone.utc),
            "items": items,
            "controls": {
                name: os.getenv(name, "false").lower() == "true"
                for name in (
                    "AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED",
                    "FACTORY_RESERVATION_REVIEW_ENABLED",
                    "FACTORY_INVOKE_FAILURE_CESSATION_ENABLED",
                    "AGENT_RESPONSE_LOST_RECOVERY_ENABLED",
                )
            },
            "coverage": "Persisted metadata only. Receipt presence does not establish validity or adoption. Guest cessation is not verified.",
            "recovery_authorized": False,
        }
    )


def find_routine_permits(job_name, *, offset=0, limit=20, session=None):
    """Locate exact routine ownership without exporting job payloads or results."""
    if (
        not isinstance(job_name, str)
        or not 1 <= len(job_name) <= 256
        or job_name != job_name.strip()
        or type(offset) is not int
        or not 0 <= offset <= 1000000
        or type(limit) is not int
        or not 1 <= limit <= 20
    ):
        raise ValueError("Supply an exact routine job name and bounded pagination")
    if session is None:
        with Session(get_engine(), autoflush=False) as db:
            return find_routine_permits(
                job_name, offset=offset, limit=limit, session=db
            )
    rows = _rows(
        session,
        """
        SELECT id, session_id, pending_seq, tier, state, outcome, created_at, settled_at
        FROM agent_sessions.capacity_reservations
        WHERE routine_job_name = :name
        ORDER BY id DESC LIMIT :limit OFFSET :offset
        """,
        {"name": job_name, "limit": limit + 1, "offset": offset},
    )
    return _safe(
        {
            "observed_at": datetime.now(timezone.utc),
            "job_name": job_name,
            "items": rows[:limit],
            "next_offset": offset + limit if len(rows) > limit else None,
            "coverage": "Persisted permits only. Empty results do not prove non-dispatch or cessation.",
            "recovery_authorized": False,
        }
    )


def inspect_session_turn(session_id, seq, *, session=None):
    """Inspect one persisted dispatch identity, including pre-permit queues."""
    if any(type(v) is not int or not 1 <= v <= 2**63 - 1 for v in (session_id, seq)):
        raise ValueError("Supply positive session ID and turn sequence")
    if session is None:
        with Session(get_engine(), autoflush=False) as db:
            return inspect_session_turn(session_id, seq, session=db)
    db = session
    params = {"id": session_id, "seq": seq}
    agents = _rows(
        db,
        """
        SELECT id, status, admission_tier, workflow_id, ember_session_id,
               created_at, last_turn_at,
               result_receipt_fence_id IS NOT NULL AS result_receipt_fenced,
               guest_cleanup_id IS NOT NULL AS cleanup_pending
        FROM agent_sessions.agent_sessions WHERE id=:id
    """,
        params,
    )
    pending = _rows(
        db,
        """
        SELECT id, seq, created_at, claimed_by_replica, claimed_at,
               dispatch_count, last_dispatch_at, capacity_state,
               capacity_started_at, capacity_next_poll_at
        FROM agent_sessions.pending_messages WHERE session_id=:id AND seq=:seq
    """,
        params,
    )
    turns = _rows(
        db,
        """
        SELECT seq, terminal_reason, stop_reason, created_at
        FROM agent_sessions.agent_turns WHERE session_id=:id AND seq=:seq
    """,
        params,
    )
    permits = _rows(
        db,
        """
        SELECT id FROM agent_sessions.capacity_reservations
        WHERE session_id=:id AND pending_seq=:seq ORDER BY id DESC LIMIT 21
    """,
        params,
    )
    ids = [row["id"] for row in permits[:20]]
    details = inspect_permits(ids, session=db) if ids else {"items": []}
    return _safe(
        {
            "observed_at": datetime.now(timezone.utc),
            "session_id": session_id,
            "seq": seq,
            "found": bool(agents),
            "session": next(iter(agents), None),
            "pending": next(iter(pending), None),
            "turn": next(iter(turns), None),
            "permits": details["items"],
            "permits_truncated": len(permits) > 20,
            "coverage": "Persisted metadata only. Reads can race with dispatch. Missing records do not prove non-dispatch or cessation. No guest contacted.",
            "recovery_authorized": False,
        }
    )


def inspect_raw_extraction(job_name, *, session=None):
    """Read bounded outcome metadata for one exact content-addressed raw job."""
    if (
        not isinstance(job_name, str)
        or re.fullmatch(r"kg:[0-9a-f]{64}", job_name) is None
    ):
        raise ValueError("Supply one exact kg:<64 lowercase hex characters> job ID")
    if session is None:
        with Session(get_engine(), autoflush=False) as db:
            return inspect_raw_extraction(job_name, session=db)
    from knowledge.api import EXTRACTION_VERSION

    db = session
    params = {"raw": job_name[3:], "job": job_name, "version": EXTRACTION_VERSION}
    # Only project the numeric counter, never the raw's content or extra object.
    if db.bind.dialect.name == "sqlite":
        doc = "CASE WHEN json_valid(extra) THEN extra ELSE '{}' END"
        passes = f"CASE WHEN json_type({doc}, '$.extraction_passes') = 'integer' THEN json_extract({doc}, '$.extraction_passes') END"
    else:
        passes = "CASE WHEN jsonb_typeof(extra->'extraction_passes') = 'number' THEN extra->'extraction_passes' END"
    raw = _rows(
        db,
        f"SELECT id, {passes} AS passes FROM knowledge.raw_inputs WHERE raw_id=:raw",
        params,
    )
    result = {
        "observed_at": datetime.now(timezone.utc),
        "job_name": job_name,
        "raw_found": bool(raw),
        "extraction_version": EXTRACTION_VERSION,
        "extraction_passes": None,
        "provenance": None,
        "attempts": [],
        "coverage": "Persisted metadata only; reads may race. Provenance is scoped to the current extraction version, not attributed to an attempt. Absence is not proof of no effects or cessation.",
        "recovery_authorized": False,
    }
    if raw:
        count = raw[0]["passes"]
        result["extraction_passes"] = (
            count if type(count) is int and 0 <= count <= 2**63 - 1 else None
        )
        params["raw_fk"] = raw[0]["id"]
        rows = _rows(
            db,
            """
            SELECT created_at, error IS NOT NULL AS has_error,
                   atom_fk IS NOT NULL AS has_atom
            FROM knowledge.atom_raw_provenance
            WHERE raw_fk=:raw_fk AND gardener_version=:version
            ORDER BY id DESC LIMIT 1001
        """,
            params,
        )
        bounded = rows[:1000]
        stamps = [row["created_at"] for row in bounded if row["created_at"] is not None]
        result["provenance"] = {
            "rows_observed": len(bounded),
            "truncated": len(rows) > 1000,
            "error_rows": sum(bool(row["has_error"]) for row in bounded),
            "atom_rows_without_error": sum(
                bool(row["has_atom"]) and not row["has_error"] for row in bounded
            ),
            "first_observed_at": min(stamps) if stamps else None,
            "last_observed_at": max(stamps) if stamps else None,
        }
    attempts = _rows(
        db,
        """
        SELECT p.id AS permit_id, p.session_id, p.pending_seq,
               p.created_at, p.settled_at, p.state,
               a.status AS session_status, t.terminal_reason, t.created_at AS turn_at
        FROM agent_sessions.capacity_reservations AS p
        LEFT JOIN agent_sessions.agent_sessions AS a ON a.id=p.session_id
        LEFT JOIN agent_sessions.agent_turns AS t
          ON t.session_id=p.session_id AND t.seq=p.pending_seq
        WHERE p.routine_job_name=:job AND p.tier='kg'
        ORDER BY p.id DESC LIMIT 6
    """,
        params,
    )
    # Even status columns are untrusted strings. Never echo free-form failures.
    allowed = {
        "state": {"reserved", "running", "uncertain", "settled"},
        "session_status": {
            "pending",
            "running",
            "completed",
            "failed",
            "warn",
            "idle",
            "cancelled",
        },
        "terminal_reason": {
            "success",
            "error",
            "cancelled",
            "complete",
            "completed",
            "end_turn",
        },
    }
    for attempt in attempts[:5]:
        for key, values in allowed.items():
            if attempt[key] is not None and attempt[key] not in values:
                attempt[key] = "unrecognized"
    result["attempts"] = attempts[:5]
    result["attempts_truncated"] = len(attempts) > 5
    return _safe(result)
