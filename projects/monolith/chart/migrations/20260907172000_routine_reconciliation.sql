-- Operator evidence survives routine-job deletion and preserves unknown turns.
CREATE TABLE claude_agent.routine_reconciliations (
    reconciliation_key TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    actor TEXT NOT NULL,
    job_name TEXT NOT NULL,
    session_id INTEGER NOT NULL,
    unknown_turn_seq INTEGER NOT NULL CHECK (unknown_turn_seq > 0),
    disposition TEXT NOT NULL CHECK (disposition IN ('rearm', 'retain_applied')),
    evidence_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, unknown_turn_seq)
);
CREATE FUNCTION claude_agent.routine_reconciliation_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'routine reconciliation audit is append only';
END;
$$;
CREATE TRIGGER routine_reconciliation_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON claude_agent.routine_reconciliations
    FOR EACH STATEMENT EXECUTE FUNCTION claude_agent.routine_reconciliation_append_only();
