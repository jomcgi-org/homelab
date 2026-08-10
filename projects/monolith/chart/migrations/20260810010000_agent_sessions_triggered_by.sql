-- The email of the human who triggered the session, projected from the
-- X-Auth-Email header. NULL for Discord, MCP, and workflow-started sessions.

ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN triggered_by TEXT;
