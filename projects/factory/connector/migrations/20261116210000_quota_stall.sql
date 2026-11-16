-- quota_stall cancels a turn that went quiet while the broker reads the
-- Codex quota exhausted (HOME-127). event_count and event_at record when the
-- connector last saw the attempt's kagent task change.
ALTER TABLE cancellation DROP CONSTRAINT cancellation_reason_check;
ALTER TABLE cancellation ADD CONSTRAINT cancellation_reason_check
    CHECK (reason IN ('run_canceled', 'run_closed', 'reassignment', 'round_stopped', 'quota_stall'));
ALTER TABLE delivery_attempt
    ADD COLUMN event_count integer NOT NULL DEFAULT 0,
    ADD COLUMN event_at timestamptz;
