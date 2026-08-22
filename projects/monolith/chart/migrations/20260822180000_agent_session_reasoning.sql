-- Per-session qwen thinking control (#5051). A qwen (pi lane) session runs with
-- thinking off by default so short turns skip the reasoning trace; setting this
-- flag at session start makes every turn of the session opt back into full
-- reasoning, for hard or long agentic tasks that need it. The value is sent to
-- the guest on each invoke as the pi thinking level, so it must persist to
-- survive a replica handoff mid-session. Non-qwen sessions ignore it.
ALTER TABLE agent_sessions.agent_sessions ADD COLUMN reasoning BOOLEAN NOT NULL DEFAULT FALSE;
