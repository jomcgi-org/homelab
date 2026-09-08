ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN result_receipt_fence_id text;

ALTER TABLE agent_sessions.result_receipts
    ADD COLUMN response_observed_at timestamptz;
