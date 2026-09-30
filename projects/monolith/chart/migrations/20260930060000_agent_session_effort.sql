-- The Claude CLI effort a session's turns run at (#6461). The factory sets it
-- from the node's role; NULL leaves the guest shim's per-model default, which
-- is what every session created before this column (and every interactive
-- session) gets. The shim validates the level, so no CHECK is repeated here.
ALTER TABLE agent_sessions.agent_sessions ADD COLUMN effort TEXT;
