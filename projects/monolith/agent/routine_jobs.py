"""Operations over ``claude_agent.routine_jobs``, delegated work for cloud Routines.

These functions back the ``monolith-agent-*-routine-job`` MCP tools and the
in-cluster Luna drainer. Unlike ``scheduler.api``'s ``scheduled_jobs``, these
rows use explicit SKIP LOCKED claims so either consumer can safely lease work.
Completing a one-shot row clears ``next_run_at``; ``trigger_job`` explicitly
re-arms it by setting ``next_run_at = now()``.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import bindparam, text
from sqlmodel import Session

from shared.invocation_outcomes import UNKNOWN_INVOCATION
from core.db import get_engine


_ROW_COLUMNS = (
    "name",
    "routine_kind",
    "interval_secs",
    "next_run_at",
    "last_run_at",
    "last_status",
    "last_summary",
    "locked_by",
    "locked_at",
    "ttl_secs",
    "payload",
    "created_by",
    "created_at",
)

# While ordinary work is due, admit at most one preferred repository freshness
# job per five minutes. Completion/claim timestamps persist through DBOS cycle
# continuation and restart, so a new workflow cannot reset the fairness window.
REPO_FRESHNESS_COOLDOWN_SECONDS = 300


def _repo_freshness_sql(alias: str, *, sqlite: bool) -> str:
    raw_table = "raw_inputs" if sqlite else "knowledge.raw_inputs"
    raw_id = (
        f"json_extract(CASE WHEN json_valid({alias}.payload) "
        f"THEN {alias}.payload ELSE '{{}}' END, '$.raw_id')"
        if sqlite
        else f"{alias}.payload ->> 'raw_id'"
    )
    return f"""(
        {alias}.routine_kind = 'kg-drain' AND (
            {alias}.name = 'kg-repo-diff' OR EXISTS (
                SELECT 1 FROM {raw_table} AS raw
                 WHERE raw.raw_id = {raw_id} AND raw.source = 'repo-diff'
            )
        )
    )"""


def _row_to_dict(row: Any) -> dict:
    return {col: getattr(row, col) for col in _ROW_COLUMNS}


def list_jobs(
    due_only: bool = False,
    kind: str | None = None,
    kinds: tuple[str, ...] | list[str] | None = None,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[dict]:
    """Return routine_jobs rows, optionally filtered to due-only and/or by kind."""
    kind_filter = "routine_kind = ANY(:kinds)" if kinds is not None else "TRUE"
    order_clause = (
        "created_at DESC, name" if newest_first else "next_run_at NULLS LAST, name"
    )
    limit_clause = "LIMIT :limit" if limit is not None else ""
    sql = text(
        """
        SELECT name, routine_kind, interval_secs, next_run_at, last_run_at,
               last_status, last_summary, locked_by, locked_at, ttl_secs,
               payload, created_by, created_at
          FROM claude_agent.routine_jobs
         WHERE routine_kind != '_drainer-worker'
           AND (:due_only IS FALSE
                OR (next_run_at IS NOT NULL
                    AND next_run_at <= now()
                    AND (locked_by IS NULL
                         OR locked_at + (ttl_secs || ' seconds')::interval < now())))
           AND (CAST(:kind AS text) IS NULL OR routine_kind = CAST(:kind AS text))
           AND ("""
        + kind_filter
        + """)
         ORDER BY """
        + order_clause
        + "\n         "
        + limit_clause
        + """
        """
    )
    with Session(get_engine()) as session:
        params = {
            "due_only": due_only,
            "kind": kind,
            "kinds": list(kinds or []),
            "limit": limit,
        }
        rows = session.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def claim_job(
    holder: str,
    ttl_secs: int,
    kind: str | None = None,
    kinds: tuple[str, ...] | list[str] | None = None,
    name: str | None = None,
    prefer_repo_freshness: bool = False,
    *,
    session: Session | None = None,
    exclude_names: set[str] | None = None,
    recover_holder: bool = False,
) -> dict | None:
    """Claim a routine_jobs row, including its JSONB payload.

    If ``name`` is set, attempt to claim that specific row; returns None if it's
    held with a still-live lock. If ``name`` is None, claim the next due
    unclaimed row, optionally filtered by ``kind``. Uses ``SELECT FOR UPDATE
    SKIP LOCKED`` so concurrent claimers never block each other.

    A supplied session composes the claim with admission and leaves commit or
    rollback to its caller. The drainer serializes these claims under the shared
    capacity lock so parallel workers preserve the freshness cooldown.

    The drainer opts into repository freshness preference. When both
    classes are due, an oldest-due scout or persisted repo-diff raw gets the
    next claim after the cooldown. Ordinary FIFO work takes precedence during
    the cooldown. Either class can use an otherwise idle slot. This changes
    ordering only, never due, lease, kind or unknown-outcome eligibility.
    """
    engine = get_engine()
    sqlite = engine.dialect.name == "sqlite"
    table = "routine_jobs" if sqlite else "claude_agent.routine_jobs"
    now_expr = "CURRENT_TIMESTAMP" if sqlite else "now()"
    expired_expr = (
        "datetime(locked_at, '+' || ttl_secs || ' seconds') < CURRENT_TIMESTAMP"
        if sqlite
        else "locked_at + (ttl_secs || ' seconds')::interval < now()"
    )
    if name is not None:
        select_sql = text(
            f"""
            SELECT name, locked_by, locked_at, ttl_secs
              FROM {table}
             WHERE name = :name AND routine_kind != '_drainer-worker'
               AND (last_status IS NULL OR last_status != :unknown_outcome)
             {"" if sqlite else "FOR UPDATE"}
            """
        )
    else:
        if kinds and sqlite:
            kinds_filter = "routine_kind IN :kinds"
        elif kinds:
            kinds_filter = "routine_kind = ANY(:kinds)"
        else:
            kinds_filter = "TRUE"
        order = "next_run_at ASC, name"
        if prefer_repo_freshness:
            cutoff = (
                "datetime(CURRENT_TIMESTAMP, '-' || :freshness_cooldown || ' seconds')"
                if sqlite
                else "now() - (:freshness_cooldown || ' seconds')::interval"
            )
            recent_freshness = f"""EXISTS (
                SELECT 1 FROM {table} AS recent
                 WHERE {_repo_freshness_sql("recent", sqlite=sqlite)}
                   AND (recent.last_run_at >= {cutoff}
                        OR recent.locked_at >= {cutoff})
            )"""
            order = (
                f"CASE WHEN {_repo_freshness_sql('candidate', sqlite=sqlite)} "
                f"= (NOT {recent_freshness}) THEN 0 ELSE 1 END, " + order
            )
        select_sql = text(
            f"""
            SELECT name, locked_by, locked_at, ttl_secs
              FROM {table} AS candidate
             WHERE next_run_at IS NOT NULL
               AND routine_kind != '_drainer-worker'
               AND (last_status IS NULL OR last_status != :unknown_outcome)
               AND next_run_at <= {now_expr}
               {"AND name NOT IN :excluded_names" if exclude_names else ""}
               AND (
                    locked_by IS NULL
                    OR ({expired_expr} AND locked_by NOT LIKE 'luna-drainer:%')
               )
               AND (CAST(:kind AS text) IS NULL OR routine_kind = CAST(:kind AS text))
               AND ("""
            + kinds_filter
            + f""")
             ORDER BY {order}
             LIMIT 1
             {"" if sqlite else "FOR UPDATE SKIP LOCKED"}
            """
        )
        if kinds and sqlite:
            select_sql = select_sql.bindparams(bindparam("kinds", expanding=True))
        if exclude_names:
            select_sql = select_sql.bindparams(
                bindparam("excluded_names", expanding=True)
            )

    update_sql = text(
        f"""
        UPDATE {table}
           SET locked_by = :holder,
               locked_at = {now_expr},
               ttl_secs = :ttl
         WHERE name = :name AND routine_kind != '_drainer-worker'
           AND (last_status IS NULL OR last_status != :unknown_outcome)
        RETURNING name, routine_kind, interval_secs, next_run_at, last_run_at,
                  last_status, last_summary, locked_by, locked_at, ttl_secs,
                  payload, created_by, created_at
        """
    )

    owns_session = session is None
    with Session(engine) if owns_session else nullcontext(session) as session:
        if recover_holder:
            prior = session.execute(
                text(
                    f"SELECT {', '.join(_ROW_COLUMNS)} FROM {table} "
                    "WHERE locked_by = :holder AND routine_kind != '_drainer-worker' "
                    "AND (last_status IS NULL OR last_status != :unknown_outcome)"
                ),
                {"holder": holder, "unknown_outcome": UNKNOWN_INVOCATION},
            ).all()
            if len(prior) > 1:
                raise ValueError("ambiguous routine claim identity")
            if prior:
                recovered = prior[0]
                if (
                    (kind is not None and recovered.routine_kind != kind)
                    or (kinds is not None and recovered.routine_kind not in kinds)
                    or (name is not None and recovered.name != name)
                ):
                    return None
                return _row_to_dict(recovered)
        if name is not None:
            row = session.execute(
                select_sql, {"name": name, "unknown_outcome": UNKNOWN_INVOCATION}
            ).first()
        else:
            row = session.execute(
                select_sql,
                {
                    "kind": kind,
                    "kinds": list(kinds or []),
                    "excluded_names": sorted(exclude_names or []),
                    "unknown_outcome": UNKNOWN_INVOCATION,
                    "freshness_cooldown": REPO_FRESHNESS_COOLDOWN_SECONDS,
                },
            ).first()

        if row is None:
            if owns_session:
                session.rollback()
            return None

        # Deterministic drainer claims remain owned through postprocessing,
        # even after turn settlement and TTL expiry. Only their holder may
        # replay; abandonment needs explicit terminal-owner reconciliation.
        if row.locked_by and row.locked_by.startswith("luna-drainer:"):
            return None
        # Legacy leases retain their original TTL behavior.
        if (
            row.locked_by is not None
            and row.locked_at is not None
            and row.ttl_secs is not None
        ):
            if sqlite:
                still_live = session.execute(
                    text(
                        "SELECT datetime(:locked_at, '+' || :ttl || ' seconds') "
                        "> CURRENT_TIMESTAMP AS live"
                    ),
                    {"locked_at": row.locked_at, "ttl": row.ttl_secs},
                ).scalar()
            else:
                still_live = session.execute(
                    text(
                        "SELECT (:locked_at + (:ttl || ' seconds')::interval) "
                        "> now() AS live"
                    ),
                    {"locked_at": row.locked_at, "ttl": row.ttl_secs},
                ).scalar()
            if still_live:
                if owns_session:
                    session.rollback()
                return None

        claimed = session.execute(
            update_sql,
            {
                "holder": holder,
                "ttl": ttl_secs,
                "name": row.name,
                "unknown_outcome": UNKNOWN_INVOCATION,
            },
        ).first()
        if owns_session:
            session.commit()

    return _row_to_dict(claimed) if claimed else None


def _normalize_locked_at(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def hold_job_for_unknown_outcome(
    name: str,
    session_id: int,
    summary: str,
    *,
    expected_locked_by: str | None = None,
    expected_locked_at: datetime | str | None = None,
) -> bool:
    """Retain the job and its payload while disabling automatic re-admission."""
    engine = get_engine()
    sqlite = engine.dialect.name == "sqlite"
    table = "routine_jobs" if sqlite else "claude_agent.routine_jobs"
    now_expr = "CURRENT_TIMESTAMP" if sqlite else "now()"
    guarded = expected_locked_by is not None and expected_locked_at is not None
    if guarded:
        expected_at = _normalize_locked_at(expected_locked_at)
        # SQLite's CURRENT_TIMESTAMP is stored without an offset, while a
        # value round-tripped through DBOS is normally ISO-8601. Normalize the
        # bound value to the representation used by SQLite's datetime type.
        if sqlite:
            expected_at = expected_at.replace(tzinfo=None)
            expected_at = expected_at.strftime(
                "%Y-%m-%d %H:%M:%S.%f"
                if expected_at.microsecond
                else "%Y-%m-%d %H:%M:%S"
            )
    else:
        expected_at = None
    with Session(engine) as session:
        where = "WHERE name = :name AND routine_kind != '_drainer-worker'"
        params = {
            "name": name,
            "status": UNKNOWN_INVOCATION,
            "summary": f"session_id={session_id}: {summary}",
        }
        if guarded:
            where += (
                " AND locked_by = :expected_locked_by"
                " AND locked_at = :expected_locked_at"
            )
            params.update(
                {
                    "expected_locked_by": expected_locked_by,
                    "expected_locked_at": expected_at,
                }
            )
        result = session.execute(
            text(f"""
                UPDATE {table}
                   SET next_run_at = NULL, last_run_at = {now_expr},
                       last_status = :status, last_summary = :summary,
                       locked_by = NULL, locked_at = NULL
                 {where}
            """),
            params,
        )
        if result.rowcount == 0 and guarded:
            row = session.execute(
                text(
                    f"SELECT last_status, next_run_at, last_summary, "
                    f"locked_by, locked_at FROM {table} "
                    "WHERE name = :name AND routine_kind != '_drainer-worker'"
                ),
                {"name": name},
            ).first()
            session.rollback()
            return bool(
                row
                and row.last_status == UNKNOWN_INVOCATION
                and row.next_run_at is None
                and row.locked_by is None
                and row.locked_at is None
                and isinstance(row.last_summary, str)
                and row.last_summary.startswith(f"session_id={session_id}:")
            )
        session.commit()
        return result.rowcount > 0


def lock_claim(session: Session, name: str, expected_holder: str | None) -> bool:
    """Compare and lock an exact claim owner for its whole mutation transaction."""
    if expected_holder is None:
        return True
    table = (
        "routine_jobs"
        if session.get_bind().dialect.name == "sqlite"
        else "claude_agent.routine_jobs"
    )
    result = session.execute(
        text(
            f"UPDATE {table} SET locked_by=locked_by WHERE name=:name "
            "AND locked_by=:holder AND routine_kind != '_drainer-worker'"
        ),
        {"name": name, "holder": expected_holder},
    )
    return result.rowcount == 1


def complete_job(
    name: str,
    status: str,
    summary: str | None = None,
    *,
    expected_holder: str | None = None,
    deregister: bool = False,
    preserve_repo_freshness: bool = False,
    defer_seconds: int | None = None,
) -> bool:
    """Mark a job complete.

    Sets ``last_run_at = now()``, ``last_status``, and (if provided)
    ``last_summary``; clears the lock fields. If ``interval_secs`` is non-null
    on the row, advances ``next_run_at`` by that many seconds from now.
    One-shot rows clear ``next_run_at`` and remain idle until ``trigger_job``
    re-arms them.
    """
    engine = get_engine()
    sqlite = engine.dialect.name == "sqlite"
    table = "routine_jobs" if sqlite else "claude_agent.routine_jobs"
    now_expr = "CURRENT_TIMESTAMP" if sqlite else "now()"
    next_expr = (
        "datetime(CURRENT_TIMESTAMP, '+' || interval_secs || ' seconds')"
        if sqlite
        else "now() + (interval_secs || ' seconds')::interval"
    )
    sql = text(
        f"""
        UPDATE {table}
           SET last_run_at = {now_expr},
               last_status = :status,
               last_summary = COALESCE(:summary, last_summary),
               locked_by = NULL,
               locked_at = NULL,
               next_run_at = CASE
                   WHEN interval_secs IS NOT NULL
                   THEN {next_expr}
                   ELSE NULL
               END
         WHERE name = :name AND routine_kind != '_drainer-worker'
           AND (last_status IS NULL OR last_status != :unknown_outcome)
        """
    )
    with Session(get_engine()) as session:
        if not lock_claim(session, name, expected_holder):
            return False
        result = session.execute(
            sql,
            {
                "name": name,
                "status": status,
                "summary": summary,
                "unknown_outcome": UNKNOWN_INVOCATION,
            },
        )
        if result.rowcount and defer_seconds is not None:
            deferred = (
                "datetime(CURRENT_TIMESTAMP, '+' || :seconds || ' seconds')"
                if sqlite
                else "now() + (:seconds || ' seconds')::interval"
            )
            session.execute(
                text(f"UPDATE {table} SET next_run_at={deferred} WHERE name=:name"),
                {"name": name, "seconds": defer_seconds},
            )
        if result.rowcount and deregister:
            retention = (
                f"AND NOT {_repo_freshness_sql('candidate', sqlite=sqlite)}"
                if preserve_repo_freshness
                else ""
            )
            session.execute(
                text(f"DELETE FROM {table} AS candidate WHERE name=:name {retention}"),
                {"name": name},
            )
        session.commit()
    return result.rowcount > 0


def register_job(
    name: str,
    kind: str,
    interval_secs: int | None = None,
    payload: dict | None = None,
    next_run_at: datetime | None = None,
    created_by: str = "unknown",
) -> bool:
    """Insert a new routine_jobs row. Raises ``IntegrityError`` if name exists."""
    sql = text(
        """
        INSERT INTO claude_agent.routine_jobs
            (name, routine_kind, interval_secs, next_run_at, payload, created_by)
        VALUES
            (:name, :kind, :interval_secs, :next_run_at,
             CAST(:payload AS JSONB), :created_by)
        """
    )
    if name.startswith("_drainer-worker:") or kind == "_drainer-worker":
        raise ValueError("reserved drainer worker identity")
    payload_json = json.dumps(payload) if payload is not None else None
    with Session(get_engine()) as session:
        session.execute(
            sql,
            {
                "name": name,
                "kind": kind,
                "interval_secs": interval_secs,
                "next_run_at": next_run_at,
                "payload": payload_json,
                "created_by": created_by,
            },
        )
        session.commit()
    return True


def deregister_job(
    name: str,
    *,
    preserve_repo_freshness: bool = False,
    expected_holder: str | None = None,
) -> bool:
    """Remove a job unless it retains an unresolved invocation outcome.

    The drainer retains completed repository freshness rows as cooldown
    evidence. Their next_run_at is already NULL, so retention does not rearm
    them. Explicit operator deregistration keeps its existing behavior.
    """
    engine = get_engine()
    sqlite = engine.dialect.name == "sqlite"
    table = "routine_jobs" if sqlite else "claude_agent.routine_jobs"
    retention = (
        f"AND NOT {_repo_freshness_sql('candidate', sqlite=sqlite)}"
        if preserve_repo_freshness
        else ""
    )
    sql = text(f"""
        DELETE FROM {table} AS candidate WHERE name = :name AND routine_kind != '_drainer-worker'
          AND (last_status IS NULL OR last_status != :unknown_outcome)
          {retention}
    """)
    with Session(engine) as session:
        if not lock_claim(session, name, expected_holder):
            return False
        result = session.execute(
            sql, {"name": name, "unknown_outcome": UNKNOWN_INVOCATION}
        )
        session.commit()
    return result.rowcount > 0


def trigger_job(name: str) -> bool:
    """Re-arm a row unless its invocation outcome is held for reconciliation."""
    engine = get_engine()
    sqlite = engine.dialect.name == "sqlite"
    table = "routine_jobs" if sqlite else "claude_agent.routine_jobs"
    now_expr = "CURRENT_TIMESTAMP" if sqlite else "now()"
    sql = text(f"""
        UPDATE {table} SET next_run_at = {now_expr} WHERE name = :name AND routine_kind != '_drainer-worker'
          AND (last_status IS NULL OR last_status != :unknown_outcome)
    """)
    with Session(engine) as session:
        result = session.execute(
            sql, {"name": name, "unknown_outcome": UNKNOWN_INVOCATION}
        )
        session.commit()
    return result.rowcount > 0


def defer_job(name: str, seconds: int, *, expected_holder: str | None = None) -> bool:
    """Re-arm a job after ``seconds`` while clearing any active claim."""
    engine = get_engine()
    if engine.dialect.name == "sqlite":
        table = "routine_jobs"
        deferred_expr = "datetime(CURRENT_TIMESTAMP, '+' || :seconds || ' seconds')"
    else:
        table = "claude_agent.routine_jobs"
        deferred_expr = "now() + (:seconds || ' seconds')::interval"
    sql = text(
        f"""
        UPDATE {table}
           SET next_run_at = {deferred_expr},
               locked_by = NULL,
               locked_at = NULL
         WHERE name = :name AND routine_kind != '_drainer-worker'
           AND (last_status IS NULL OR last_status != :unknown_outcome)
        """
    )
    with Session(get_engine()) as session:
        if not lock_claim(session, name, expected_holder):
            return False
        result = session.execute(
            sql,
            {"name": name, "seconds": seconds, "unknown_outcome": UNKNOWN_INVOCATION},
        )
        session.commit()
    return result.rowcount > 0


def update_job_payload(
    name: str, payload: dict, *, expected_holder: str | None = None
) -> bool:
    """Replace a job payload, preserving the rest of its claim state."""
    engine = get_engine()
    sqlite = engine.dialect.name == "sqlite"
    table = "routine_jobs" if sqlite else "claude_agent.routine_jobs"
    payload_expr = ":payload" if sqlite else "CAST(:payload AS JSONB)"
    sql = text(
        f"UPDATE {table} SET payload = {payload_expr} WHERE name = :name AND routine_kind != '_drainer-worker'"
    )
    with Session(engine) as session:
        if not lock_claim(session, name, expected_holder):
            return False
        result = session.execute(sql, {"name": name, "payload": json.dumps(payload)})
        session.commit()
    return result.rowcount > 0


DRAINER_WORKER_COUNT = 2
_DRAINER_WORKER_KIND = "_drainer-worker"
_DRAINER_WORKER_PREFIX = "_drainer-worker:"


def drainer_worker_intents(*, session: Session | None = None) -> dict[str, dict]:
    """Read the two enqueue intents. These idle metadata rows are never jobs."""
    engine = get_engine()
    table = (
        "routine_jobs"
        if engine.dialect.name == "sqlite"
        else "claude_agent.routine_jobs"
    )
    with Session(engine) if session is None else nullcontext(session) as db:
        rows = db.execute(
            text(f"SELECT name, payload FROM {table} WHERE routine_kind = :kind"),
            {"kind": _DRAINER_WORKER_KIND},
        ).all()
        result = {}
        for row in rows:
            payload = (
                json.loads(row.payload) if isinstance(row.payload, str) else row.payload
            )
            if (
                row.name
                not in {
                    f"{_DRAINER_WORKER_PREFIX}{i}" for i in range(DRAINER_WORKER_COUNT)
                }
                or not isinstance(payload, dict)
                or not isinstance(payload.get("workflow_id"), str)
                or type(payload.get("generation")) is not int
                or payload["generation"] < 1
                or payload["workflow_id"] != f"{row.name}:{payload['generation']}"
                or set(payload) != {"workflow_id", "generation"}
            ):
                raise ValueError("invalid drainer worker intent")
            result[row.name] = payload
        return result


def reserve_drainer_workers(
    expected: dict[str, dict],
    statuses: dict[str, str | None],
    live_workflow_ids: set[str],
    *,
    completing_workflow_id: str | None = None,
) -> list[str] | None:
    """Persist a bounded deficit before enqueue, using exact observed identities.

    None means the observed snapshot changed and the caller must reread it,
    not checkpoint a successful empty handoff.
    An absent DBOS row reuses its original intent, never a fresh workflow id.
    Unknown statuses and stale observations cannot retire an intent. Only a
    confirmed terminal workflow, or this workflow's own completed job loop,
    can make its slot available. No lease expiration releases worker slots.
    """
    from agent_sessions.admission import lock_pool

    terminal = {"SUCCESS", "ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}
    engine = get_engine()
    sqlite = engine.dialect.name == "sqlite"
    table = "routine_jobs" if sqlite else "claude_agent.routine_jobs"
    with Session(engine) as session:
        lock_pool(session)
        current = drainer_worker_intents(session=session)
        if current != expected:
            return None
        ids = {value["workflow_id"] for value in current.values()}
        if any(wid in live_workflow_ids and statuses.get(wid) is None for wid in ids):
            raise ValueError("inconsistent drainer workflow inventory")
        if any(wid not in statuses for wid in ids):
            raise ValueError("incomplete drainer worker inventory")
        if any(
            status not in terminal | {"PENDING", "ENQUEUED", None}
            for status in statuses.values()
        ):
            raise ValueError("unknown drainer workflow status")
        # Old untracked cycles also occupy the same two-worker fleet during a
        # rollout. Their actual terminal state must be observed before refill.
        retired = set()
        for wid in live_workflow_ids:
            slot, _, generation_text = wid.rpartition(":")
            lineage = current.get(slot)
            if lineage is None:
                continue
            try:
                generation = int(generation_text)
            except ValueError:
                continue
            if (
                generation_text == str(generation)
                and 0 < generation < lineage["generation"]
            ):
                # This exact slot advanced past the old generation only after
                # terminal proof or its own finished job loop. A finishing
                # predecessor and its queued successor occupy one worker slot.
                retired.add(wid)
        legacy_live = live_workflow_ids - ids - retired - {completing_workflow_id}
        occupied = len(legacy_live)
        available = []
        retry = []
        for i in range(DRAINER_WORKER_COUNT):
            name = f"{_DRAINER_WORKER_PREFIX}{i}"
            previous = current.get(name)
            if previous is None:
                available.append((name, None))
                continue
            wid = previous["workflow_id"]
            status = statuses[wid]
            if wid == completing_workflow_id or status in terminal:
                available.append((name, previous))
            else:
                occupied += 1
                if status is None:
                    retry.append(wid)
        remaining = max(0, DRAINER_WORKER_COUNT - occupied)
        for name, previous in available[:remaining]:
            generation = previous["generation"] + 1 if previous else 1
            wid = f"{name}:{generation}"
            payload = json.dumps({"generation": generation, "workflow_id": wid})
            cast = ":payload" if sqlite else "CAST(:payload AS JSONB)"
            result = session.execute(
                text(f"""INSERT INTO {table} (name, routine_kind, payload, created_by)
                    VALUES (:name, :kind, {cast}, 'drainer:reconciler')
                    ON CONFLICT (name) DO UPDATE SET payload = excluded.payload
                    WHERE {table}.routine_kind = :kind"""),
                {"name": name, "kind": _DRAINER_WORKER_KIND, "payload": payload},
            )
            if result.rowcount != 1:
                raise ValueError("drainer worker identity collision")
            retry.append(wid)
        session.commit()
        return retry
