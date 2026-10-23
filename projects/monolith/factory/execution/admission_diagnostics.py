"""Bounded, SELECT-only permit metadata. Never infer permission to recover work."""

from datetime import datetime, timezone

from sqlalchemy import text
from sqlmodel import Session

from core.db import get_engine


def _rows(db, sql, params):
    if db.bind.dialect.name == "sqlite":
        for schema in ("agent_sessions.", "claude_agent.", "dbos."):
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
            "coverage": "Persisted metadata only. Guest cessation is not verified.",
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
