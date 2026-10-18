"""Fixed catalog inventory for the feature retirement in PR 7134.

No application rows, SQL bodies, grants, DDL, or caller-selected objects.
The dependency closure is conservative: owning relations of dependent rules,
defaults, constraints and triggers are included for inspection, not classified
as objects that PostgreSQL would necessarily drop.
"""

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from core.db import get_engine

MANIFEST = "homelab-pr7134-16708fa99b2ab3e1fdee69d326b4949e6a3da0e0-v1"
MAX_ROWS = 1024
MAX_BYTES = 192 * 1024
RETIRED = (
    "public.demo_pg_savings",
    "public.demo_sg_savings",
    "public.bazel_query_savings",
    "public.ember_k8s_wake_event",
    "home.archives",
    "knowledge.ingest_queue",
    "observability.topology_snapshot",
    "chat.goosecracker_steering",
    "chat.goosecracker_sessions",
    "claude_agent.agent_base_snapshots",
    "claude_agent.agent_threads",
    "demo.load_scan",
    "demo.load_run",
    "semgrep.scan_perf",
    "public_api.agent_activity_daily",
    "public_api.agent_activity_now",
    "public_api.local_session_activity_daily",
)
RETAINED = (
    "home.tasks",
    "knowledge.raw_inputs",
    "chat.discord_outbox",
    "claude_agent.routine_jobs",
    "agent_sessions.agent_sessions",
    "agent_sessions.agent_turns",
    "dr_jobs.nhs_vacancies",
    "observability.stats_snapshot",
    "observability.merged_prs",
    "observability.factory_goals",
    "observability.factory_goal_issues",
    "public_api.agent_activity_snapshot",
    "public_api.factory_activity_snapshot",
)
# Only module constants enter SQL construction. The tool takes no parameters.
RELATIONS = "VALUES " + ",".join(
    f"('{name}', '{purpose}')"
    for names, purpose in ((RETIRED, "retired"), (RETAINED, "retained"))
    for name in names
)
ROOTS = (
    """
SELECT 'pg_catalog.pg_class'::regclass::oid AS classid,
       to_regclass(name)::oid AS objid, 0 AS objsubid
FROM ("""
    + RELATIONS
    + """) AS manifest(name, purpose) WHERE purpose = 'retired'
UNION SELECT 'pg_catalog.pg_proc'::regclass::oid,
       to_regprocedure('claude_agent.notify_thread_pending()')::oid, 0
UNION SELECT 'pg_catalog.pg_namespace'::regclass::oid, oid, 0
       FROM pg_catalog.pg_namespace WHERE nspname IN ('demo', 'semgrep')
"""
)
CLOSURE = (
    """
WITH RECURSIVE roots AS ("""
    + ROOTS
    + """),
walk(classid, objid, objsubid) AS (
  SELECT * FROM roots WHERE objid IS NOT NULL
  UNION
  SELECT next.* FROM walk w CROSS JOIN LATERAL (
    SELECT d.classid, d.objid, d.objsubid FROM pg_catalog.pg_depend d
    WHERE d.refclassid = w.classid AND d.refobjid = w.objid
      AND (w.objsubid = 0 OR d.refobjsubid = w.objsubid)
    UNION
    SELECT w.classid, w.objid, 0
      WHERE w.classid = 'pg_catalog.pg_class'::regclass AND w.objsubid > 0
    UNION
    SELECT 'pg_catalog.pg_class'::regclass::oid, r.ev_class, 0
      FROM pg_catalog.pg_rewrite r
      WHERE w.classid = 'pg_catalog.pg_rewrite'::regclass AND r.oid = w.objid
    UNION
    SELECT 'pg_catalog.pg_class'::regclass::oid, a.adrelid, 0
      FROM pg_catalog.pg_attrdef a
      WHERE w.classid = 'pg_catalog.pg_attrdef'::regclass AND a.oid = w.objid
    UNION
    SELECT 'pg_catalog.pg_class'::regclass::oid, c.conrelid, 0
      FROM pg_catalog.pg_constraint c
      WHERE w.classid = 'pg_catalog.pg_constraint'::regclass AND c.oid = w.objid
        AND c.conrelid <> 0
    UNION
    SELECT 'pg_catalog.pg_class'::regclass::oid, t.tgrelid, 0
      FROM pg_catalog.pg_trigger t
      WHERE w.classid = 'pg_catalog.pg_trigger'::regclass AND t.oid = w.objid
    UNION
    SELECT d.refclassid, d.refobjid, d.refobjsubid FROM pg_catalog.pg_depend d
      WHERE d.classid = w.classid AND d.objid = w.objid AND d.deptype = 'i'
        AND (w.objsubid = 0 OR d.objsubid = w.objsubid)
  ) next
)
"""
)
QUERIES = {
    "observation": """SELECT transaction_timestamp() AS observed_at,
        pg_is_in_recovery() AS is_replica,
        current_setting('transaction_read_only') AS read_only,
        current_setting('transaction_isolation') AS isolation""",
    "relations": """SELECT m.name, m.purpose, c.oid, c.relkind,
        c.oid IS NOT NULL AS present,
        CASE WHEN r.oid IS NOT NULL AND c.oid IS NOT NULL THEN
          has_table_privilege(r.oid, c.oid, 'SELECT') ELSE false END AS public_reader_select,
        CASE WHEN r.oid IS NOT NULL AND n.oid IS NOT NULL THEN
          has_schema_privilege(r.oid, n.oid, 'USAGE') ELSE false END AS public_reader_usage
        FROM ("""
    + RELATIONS
    + """) m(name, purpose)
        LEFT JOIN pg_catalog.pg_class c ON c.oid = to_regclass(m.name)
        LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_catalog.pg_roles r ON r.rolname = 'public_reader'
        ORDER BY m.purpose, m.name""",
    "special_objects": """SELECT 'function' AS kind,
        'claude_agent.notify_thread_pending()' AS name,
        to_regprocedure('claude_agent.notify_thread_pending()')::oid AS oid
        UNION ALL SELECT 'schema', names.name, n.oid
          FROM (VALUES ('demo'), ('semgrep')) names(name)
          LEFT JOIN pg_catalog.pg_namespace n ON n.nspname = names.name
        UNION ALL SELECT 'trigger', 'claude_agent.agent_threads.agent_threads_pending_notify', t.oid
          FROM (VALUES (1)) anchor(n)
          LEFT JOIN pg_catalog.pg_trigger t
          ON t.tgrelid = to_regclass('claude_agent.agent_threads')
          AND t.tgname = 'agent_threads_pending_notify'""",
    "retained_columns": """SELECT m.name AS relation, a.attnum, a.attname,
        pg_catalog.format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull
        FROM ("""
    + RELATIONS
    + """) m(name, purpose)
        JOIN pg_catalog.pg_attribute a ON a.attrelid = to_regclass(m.name)
        WHERE m.purpose = 'retained' AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY m.name, a.attnum""",
    "namespace_members": """SELECT n.nspname, d.classid, d.objid, d.objsubid,
        d.deptype, i.type, i.schema, i.name, i.identity
        FROM pg_catalog.pg_depend d
        JOIN pg_catalog.pg_namespace n ON d.refobjid = n.oid
        CROSS JOIN LATERAL pg_catalog.pg_identify_object(d.classid, d.objid, d.objsubid) i
        WHERE d.refclassid = 'pg_catalog.pg_namespace'::regclass
          AND n.nspname IN ('demo', 'semgrep')
        ORDER BY n.nspname, d.classid, d.objid, d.objsubid""",
    "dependency_closure": CLOSURE
    + """
        SELECT w.*, i.type, i.schema, i.name, i.identity
        FROM walk w CROSS JOIN LATERAL
          pg_catalog.pg_identify_object(w.classid, w.objid, w.objsubid) i
        ORDER BY w.classid, w.objid, w.objsubid""",
    "dependency_edges": CLOSURE
    + """
        SELECT d.classid, d.objid, d.objsubid, d.refclassid, d.refobjid,
          d.refobjsubid, d.deptype
        FROM walk w JOIN pg_catalog.pg_depend d
          ON d.refclassid = w.classid AND d.refobjid = w.objid
          AND (w.objsubid = 0 OR d.refobjsubid = w.objsubid)
        ORDER BY d.classid, d.objid, d.objsubid, d.refclassid, d.refobjid, d.refobjsubid""",
}
IMPLEMENTATION_SHA256 = hashlib.sha256(
    json.dumps(QUERIES, sort_keys=True).encode()
).hexdigest()


class IncompleteInventory(Exception):
    pass


def _inventory(conn):
    """Read one bounded, consistent catalog snapshot on a fresh transaction."""
    conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    conn.execute(text("SET LOCAL statement_timeout = '5s'"))
    conn.execute(text("SET LOCAL lock_timeout = '1s'"))
    conn.execute(text("SET LOCAL search_path = pg_catalog"))
    conn.execute(text("SET LOCAL idle_in_transaction_session_timeout = '5s'"))
    result = {}
    for name, sql in QUERIES.items():
        rows = (
            conn.execute(text(sql + f" LIMIT {MAX_ROWS + 1}"))
            .mappings()
            .fetchmany(MAX_ROWS + 1)
        )
        if len(rows) > MAX_ROWS:
            raise IncompleteInventory("row_limit")
        result[name] = [dict(row) for row in rows]
        if len(json.dumps(result, default=str).encode()) > MAX_BYTES:
            raise IncompleteInventory("byte_limit")
    return result


def read_inventory():
    base = {"manifest": MANIFEST, "implementation_sha256": IMPLEMENTATION_SHA256}
    try:
        # Connection closes with rollback. No caller session crosses threads and
        # no exception text can expose database URLs, SQL bodies or row values.
        with get_engine().connect() as conn:
            result = _inventory(conn)
        return {
            **base,
            "complete": True,
            "inventory": json.loads(json.dumps(result, default=str)),
        }
    except IncompleteInventory as exc:
        return {**base, "complete": False, "reason": str(exc)}
    except SQLAlchemyError:
        return {**base, "complete": False, "reason": "catalog_read_failed"}
