-- Record the list price computed from usage and model
ALTER TABLE agent_sessions.agent_turns ADD COLUMN list_cost_usd DOUBLE PRECISION;
