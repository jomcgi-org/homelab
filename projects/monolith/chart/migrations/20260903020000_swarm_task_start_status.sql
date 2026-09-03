ALTER TABLE swarm.swarm_task
    ADD COLUMN start_state TEXT NOT NULL DEFAULT 'classifying',
    ADD COLUMN start_model TEXT,
    ADD COLUMN start_payload_json TEXT,
    ADD COLUMN start_triggered_by TEXT,
    ADD COLUMN start_claim_token TEXT,
    ADD COLUMN start_updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

UPDATE swarm.swarm_task
SET start_state = CASE
    WHEN workflow_id IS NOT NULL THEN 'run'
    WHEN session_id IS NOT NULL THEN 'session'
    WHEN repo IS NULL THEN 'needs_input'
    ELSE 'error'
END;

CREATE INDEX swarm_task_start_state_idx
    ON swarm.swarm_task (start_state);
