-- Record pre-attempt branch head to enable correct compare/{base}...{head} links
ALTER TABLE agent_sessions.agent_turns ADD COLUMN base_sha TEXT;
CREATE INDEX IF NOT EXISTS agent_turns_base_sha_idx ON agent_sessions.agent_turns (base_sha);
