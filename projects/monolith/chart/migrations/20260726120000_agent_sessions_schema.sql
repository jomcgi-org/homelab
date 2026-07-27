-- Agent session store for voice-drivable Claude Code sessions via monolith MCP.

CREATE SCHEMA agent_sessions;

CREATE TABLE agent_sessions.agent_sessions (
    id SERIAL PRIMARY KEY,
    local_session_id TEXT NOT NULL UNIQUE,
    workspace TEXT NOT NULL,
    branch TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_turn_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    voice_summary TEXT
);

CREATE INDEX agent_sessions_created ON agent_sessions.agent_sessions (created_at);
CREATE INDEX agent_sessions_status ON agent_sessions.agent_sessions (status);

CREATE TABLE agent_sessions.agent_turns (
    id SERIAL PRIMARY KEY,
    session_id INT NOT NULL REFERENCES agent_sessions.agent_sessions (id),
    seq INT NOT NULL,
    prompt TEXT NOT NULL,
    voice_summary TEXT,
    result_text TEXT NOT NULL,
    terminal_reason TEXT,
    stop_reason TEXT,
    permission_denials TEXT,
    commit_sha TEXT,
    usage_json TEXT,
    cost_usd NUMERIC(10, 6),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, seq)
);

CREATE INDEX agent_turns_session ON agent_sessions.agent_turns (session_id);
CREATE INDEX agent_turns_sha ON agent_sessions.agent_turns (commit_sha);

CREATE TABLE agent_sessions.pending_messages (
    id SERIAL PRIMARY KEY,
    session_id INT NOT NULL REFERENCES agent_sessions.agent_sessions (id),
    seq INT NOT NULL,
    message_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, seq)
);

CREATE INDEX pending_messages_session ON agent_sessions.pending_messages (session_id);
