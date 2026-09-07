-- #5892: Complete agent turn pricing by recording the source of the cost figure
ALTER TABLE agent_sessions.agent_turns ADD COLUMN cost_source TEXT;
