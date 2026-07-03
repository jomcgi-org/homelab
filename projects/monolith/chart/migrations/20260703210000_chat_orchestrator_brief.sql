-- ADR 036 orchestrator brief-compiler telemetry (spec section 5). One row per
-- orchestrator call: chat and goose verdicts, and every fail-open degradation
-- (timeout, HTTP error, unparseable output). Nothing reads this table yet; the
-- token columns are the substrate for cost accounting and brief-vs-execution
-- attribution (the future ADR 037 loop). thread_id links a goose verdict to its
-- goosecracker_sessions run and is null for chat/failopen routes with no thread.
-- brief_json holds the compiled Brief (goose) or the chat reply guidance, and is
-- null on failopen. The route CHECK is mirrored in the SQLModel __table_args__
-- so the SQLite test fixtures enforce it too (create_all does not see
-- migration-only constraints).
CREATE TABLE IF NOT EXISTS chat.orchestrator_brief (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    thread_id TEXT,
    model TEXT NOT NULL DEFAULT '',
    route TEXT NOT NULL DEFAULT 'failopen',
    brief_json JSONB,
    directive_version INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cached_tokens INTEGER,
    error TEXT,
    CONSTRAINT orchestrator_brief_route_valid CHECK (route IN ('chat', 'goose', 'failopen'))
);
CREATE INDEX IF NOT EXISTS orchestrator_brief_thread ON chat.orchestrator_brief (thread_id);
CREATE INDEX IF NOT EXISTS orchestrator_brief_created ON chat.orchestrator_brief (created_at DESC);
