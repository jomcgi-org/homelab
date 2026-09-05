-- agents_writer: least-privilege identity for the monolith-agents tier (#5656).
--
-- The tier exists so that a guest-reachable MCP surface cannot reach the rest of
-- the system. Its pruned binary cannot IMPORT the private domains and its
-- ServiceAccount holds no cluster RBAC; this role is the third leg, at the
-- database layer: even with arbitrary code execution in that pod, the schemas it
-- can touch are the ones listed here and nothing else. It is never granted the
-- home, scheduler, trips, todo, hikes, ships or stars schemas.
--
-- Role creation: in production CNPG creates the role (spec.managed.roles), because
-- the Atlas migration runs as `app`, which owns the schemas but lacks CREATEROLE.
-- The DO block is a no-op there; it exists so the CNPG-less test Postgres (which
-- applies migrations as a superuser) creates the role and can exercise these
-- GRANTs. The GRANTs run as `app`, which owns every object granted.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agents_writer') THEN
        CREATE ROLE agents_writer NOLOGIN;
    END IF;
END $$;

-- Knowledge: the tier's actual working set. search_knowledge reads notes, chunks
-- and links; report_knowledge writes a raw_inputs row; dispute_fact writes a
-- disputes row. USAGE on the sequence set covers the identity columns those
-- inserts touch.
GRANT USAGE ON SCHEMA knowledge TO agents_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA knowledge TO agents_writer;
GRANT INSERT ON knowledge.raw_inputs, knowledge.disputes TO agents_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA knowledge TO agents_writer;

-- ALTER DEFAULT PRIVILEGES so a table added to knowledge later is readable
-- without a follow-up migration, the same way public_reader is kept from
-- rotting. Deliberately SELECT only: a new table becomes readable, never
-- writable, so widening the write surface stays an explicit decision.
ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge
    GRANT SELECT ON TABLES TO agents_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge
    GRANT USAGE, SELECT ON SEQUENCES TO agents_writer;

-- Extraction queue. report_knowledge does not just store a raw: ingest_raw_with_status
-- enqueues a kg-drain job in the same transaction, so without INSERT here the
-- report would roll back. SELECT is needed for the ON CONFLICT replay guard.
GRANT USAGE ON SCHEMA claude_agent TO agents_writer;
GRANT SELECT, INSERT ON claude_agent.routine_jobs TO agents_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA claude_agent TO agents_writer;

-- Discord outbox, for report_distress only. The outbox is a producer/consumer
-- split by design ("producers on any replica enqueue a row; the leader's bot
-- drains it"), so the tier needs INSERT on this ONE table and nothing else in
-- chat. Without it distress reporting fails at the database rather than at the
-- tool, which is exactly the path that must not fail quietly.
GRANT USAGE ON SCHEMA chat TO agents_writer;
GRANT SELECT, INSERT ON chat.discord_outbox TO agents_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA chat TO agents_writer;
