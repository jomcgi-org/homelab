-- Retire the unused feature schema in #7040. Repository-wide consumer and
-- migration dependency audits found only historical definitions and tests.
-- Public routes now read observability.stats_snapshot and
-- public_api.agent_activity_snapshot; neither is changed here.
--
-- nosemgrep: migration-destructive-ddl
-- Scope: only the named retired objects below and the demo/semgrep schemas.
-- Shared-schema drops use RESTRICT. Drop the known schema contents with
-- RESTRICT too, in dependency order, before allowing either schema CASCADE.
-- Refuse any remaining namespace dependency, including an unexpected function,
-- type or view, so CASCADE cannot silently remove objects outside this set.
-- Atlas applies this file transactionally: a refusal rolls back earlier drops.
-- Live inventory and deployed-consumer compatibility require operator approval
-- before rollout, with concurrent DDL in the retired schemas quiesced.
-- Historical SQL remains in the ConfigMap until a separately scoped squash.

DROP VIEW IF EXISTS public_api.agent_activity_daily RESTRICT;
DROP VIEW IF EXISTS public_api.agent_activity_now RESTRICT;
DROP VIEW IF EXISTS public_api.local_session_activity_daily RESTRICT;

DROP TABLE IF EXISTS public.demo_pg_savings RESTRICT;
DROP TABLE IF EXISTS public.demo_sg_savings RESTRICT;
DROP TABLE IF EXISTS public.bazel_query_savings RESTRICT;
DROP TABLE IF EXISTS public.ember_k8s_wake_event RESTRICT;
DROP TABLE IF EXISTS home.archives RESTRICT;
-- Owned indexes and sequences disappear with their tables.
DROP TABLE IF EXISTS knowledge.ingest_queue RESTRICT;
DROP TABLE IF EXISTS observability.topology_snapshot RESTRICT;
DROP TABLE IF EXISTS chat.goosecracker_steering RESTRICT;
DROP TABLE IF EXISTS chat.goosecracker_sessions RESTRICT;
DROP TABLE IF EXISTS claude_agent.agent_base_snapshots RESTRICT;
-- The table owns agent_threads_pending_notify, but not its trigger function.
DROP TABLE IF EXISTS claude_agent.agent_threads RESTRICT;
DROP FUNCTION IF EXISTS claude_agent.notify_thread_pending() RESTRICT;

DROP TABLE IF EXISTS demo.load_scan RESTRICT;
DROP TABLE IF EXISTS demo.load_run RESTRICT;
DROP TABLE IF EXISTS semgrep.scan_perf RESTRICT;

DO $$
DECLARE
    unexpected TEXT;
BEGIN
    SELECT string_agg(pg_catalog.pg_describe_object(d.classid, d.objid, d.objsubid),
                      ', ' ORDER BY d.classid, d.objid, d.objsubid)
    INTO unexpected
    FROM pg_catalog.pg_depend d
    JOIN pg_catalog.pg_namespace n ON n.oid = d.refobjid
    WHERE d.refclassid = 'pg_catalog.pg_namespace'::regclass
      AND n.nspname IN ('demo', 'semgrep');

    IF unexpected IS NOT NULL THEN
        RAISE EXCEPTION 'retired schema has unexpected dependencies: %', unexpected
            USING ERRCODE = '2BP01';
    END IF;
END;
$$;

DROP SCHEMA IF EXISTS demo CASCADE;
DROP SCHEMA IF EXISTS semgrep CASCADE;
