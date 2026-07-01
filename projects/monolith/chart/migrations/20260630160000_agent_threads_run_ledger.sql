-- Turns claude_agent.agent_threads into a run/result ledger for the goosecracker
-- fc-invoke executor (goose cutover PR C). The monolith now runs each goose turn
-- by POSTing to the fc-invoke daemon and awaiting the result inline, so the row
-- carries the run's identity (session_id), its captured result, an error string,
-- and the completion time. The old Firecracker placement columns (node, arch,
-- base_snapshot_ref, thread_snapshot_ref, size_bytes, wake_requested_at, ...) are
-- left in place but unused for a safe rollout; a later migration drops them.
--
-- All columns are nullable: the executor upserts session_id + task at submit and
-- fills result/result_error/completed_at only when the run finishes.

ALTER TABLE claude_agent.agent_threads
    ADD COLUMN session_id TEXT;

ALTER TABLE claude_agent.agent_threads
    ADD COLUMN result TEXT;

ALTER TABLE claude_agent.agent_threads
    ADD COLUMN result_error TEXT;

ALTER TABLE claude_agent.agent_threads
    ADD COLUMN completed_at TIMESTAMPTZ;

-- The executor looks a run up by its session id (one active row per session) to
-- update state and stamp the result, so index the lookup.
CREATE INDEX idx_agent_threads_session
    ON claude_agent.agent_threads (session_id);
