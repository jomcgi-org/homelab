ALTER TABLE agent_sessions.agent_sessions ADD COLUMN model TEXT;
ALTER TABLE agent_sessions.agent_turns ADD COLUMN model TEXT;
ALTER TABLE agent_sessions.pending_messages ADD COLUMN model TEXT;
