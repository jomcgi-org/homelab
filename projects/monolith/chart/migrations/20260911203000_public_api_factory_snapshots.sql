-- public_api snapshots for the jomcgi.dev factory activity pages (#6014).
--
-- The public tier cannot reach the factory: public_reader has no grant on the
-- swarm or agent_sessions schemas, and the factory code is pruned out of the
-- public image entirely. So the public pages read a snapshot instead. A
-- private-tier job (app/jobs_main.py factory-public-snapshot) builds the
-- payloads with the private factory code and upserts them here, and
-- agent_sessions/public_router.py reads these three tables with plain SQL.
-- Same shape as observability.topology_snapshot: whole-payload JSONB, because
-- the payload interleaves receipts, plan nodes, attempts and turn digests that
-- the public service has no code to reassemble.
--
-- Three tables rather than one, because the three pages have very different
-- sizes: the board is small and read on every visit, one task's walkthrough is
-- medium, and one session's full record (every turn with its diff) is large and
-- read only when someone opens it.

CREATE TABLE public_api.factory_activity_snapshot (
    id             SMALLINT PRIMARY KEY DEFAULT 1,
    payload        JSONB NOT NULL,
    snapshotted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT factory_activity_snapshot_singleton CHECK (id = 1)
);

CREATE TABLE public_api.factory_task_snapshot (
    issue_number   INTEGER PRIMARY KEY,
    payload        JSONB NOT NULL,
    snapshotted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- session_key is the agent_sessions.local_session_id of a factory attempt,
-- shaped factory:<task_id>:<node_key>:<attempt>. Node keys contain colons, so
-- this is an opaque key: never parse it to recover the node.
CREATE TABLE public_api.factory_session_snapshot (
    session_key    TEXT PRIMARY KEY,
    issue_number   INTEGER NOT NULL,
    payload        JSONB NOT NULL,
    snapshotted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX factory_session_snapshot_issue_number_idx
    ON public_api.factory_session_snapshot (issue_number);

-- Read access for the public tier. public_reader is created by CNPG
-- (spec.managed.roles); see 20260617000000_public_reader_role.sql. The grant is
-- on these snapshot tables only: public_reader still cannot read swarm.* or
-- agent_sessions.*, which agent_sessions/public_agent_activity_views_test.py
-- asserts.
GRANT SELECT ON public_api.factory_activity_snapshot TO public_reader;
GRANT SELECT ON public_api.factory_task_snapshot TO public_reader;
GRANT SELECT ON public_api.factory_session_snapshot TO public_reader;
