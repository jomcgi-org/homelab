-- The DBOS workflow that created this session (graph/workflows.py). NULL for
-- hand-started, Discord, and MCP sessions. Threaded explicitly rather than
-- parsed back out of local_session_id, whose "<workflow_id>-<suffix>" join
-- is ambiguous because DBOS workflow ids can themselves contain dashes.

ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN workflow_id TEXT;

CREATE INDEX agent_sessions_workflow_id
    ON agent_sessions.agent_sessions (workflow_id);
