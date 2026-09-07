-- One shared logical execution ledger, preserving the existing pending queue.
ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN admission_tier TEXT NOT NULL DEFAULT 'interactive';
UPDATE agent_sessions.agent_sessions SET admission_tier = CASE
    WHEN workflow_id IS NOT NULL AND node_key = 'kg-drain' THEN 'kg'
    WHEN workflow_id IS NOT NULL THEN 'project'
    WHEN local_session_id LIKE 'synthetic:%' THEN 'probe'
    ELSE 'interactive'
END;
ALTER TABLE agent_sessions.agent_sessions ADD CONSTRAINT agent_session_admission_tier
    CHECK (admission_tier IN ('interactive', 'project', 'kg', 'probe'));
CREATE TABLE agent_sessions.capacity_pool (id INTEGER PRIMARY KEY CHECK (id = 1));
INSERT INTO agent_sessions.capacity_pool (id) VALUES (1);
CREATE TABLE agent_sessions.capacity_reservations (
    id SERIAL PRIMARY KEY,
    local_session_id TEXT NOT NULL,
    pending_seq INTEGER NOT NULL CHECK (pending_seq > 0),
    session_id INTEGER,
    tier TEXT NOT NULL CHECK (tier IN ('interactive', 'project', 'kg', 'probe')),
    model TEXT,
    workload TEXT,
    owner TEXT,
    state TEXT NOT NULL CHECK (state IN ('reserved', 'running', 'uncertain', 'settled')),
    daily_key TEXT,
    routine_job_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ,
    outcome TEXT,
    UNIQUE (local_session_id, pending_seq),
    UNIQUE (session_id, pending_seq)
);
CREATE INDEX capacity_reservations_session_id_idx ON agent_sessions.capacity_reservations (session_id);
CREATE INDEX capacity_reservations_state_idx ON agent_sessions.capacity_reservations (state);
CREATE INDEX capacity_reservations_routine_job_name_idx ON agent_sessions.capacity_reservations (routine_job_name);
