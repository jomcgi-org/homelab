-- Store the server-owned intent separately from the protocol prompt.
ALTER TABLE agent_sessions.agent_turns ADD COLUMN prompt_intent TEXT;
