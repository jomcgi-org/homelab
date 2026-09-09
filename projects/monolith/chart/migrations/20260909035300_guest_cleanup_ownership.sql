-- Retain exact cleanup ownership while asynchronous guest teardown is observed.
-- A timeout does not expire the claim or authorize a replacement invocation.
ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN guest_cleanup_id text,
    ADD COLUMN guest_cleanup_guest_id text,
    ADD COLUMN guest_cleanup_workflow_id text,
    ADD COLUMN guest_cleanup_started_at timestamptz,
    ADD COLUMN guest_cleanup_dispatch_json text;
